"""Smart 9:16 reframing from motion and edge column energy (CPU only, no ML).

What this is, honestly: the crop follows *where pixels change and where edges
are dense*, sampled at 96 px wide and 4 fps. It is not face, person or subject
detection. It works well for a single moving subject on a calmer background
(talking head with gestures, gameplay with one action area, a product being
handled) and mis-frames when the strongest motion is not the subject (see
``LIMITATIONS``).

Pipeline (architecture plan section 5, "Subject-following crop"):

1. One ffmpeg decode of ``[start, end)`` at 4 fps feeds three branches:
   ``cropdetect`` (letterbox/pillarbox safe area), a 96 px grey
   ``tblend=difference`` motion stream and a 96 px grey ``edgedetect`` stream.
   The raw grey frames are written to files inside the caller's ``work_dir``
   (``media.run`` returns text stdout, so bytes never travel over stdout).
2. Per frame, a column profile ``E[x] = 0.7*norm(motion) + 0.3*norm(edges)``
   restricted to the safe area; a sliding window as wide as the 9:16 crop
   gives the best centre. Confidence = peak window / mean window; frames
   below 1.15 hold the previous centre.
3. Per scene segment: median (k=5), deadzone (6 % of width), zero-phase EMA,
   velocity (0.22 width/s) and acceleration (0.6 width/s^2) clamps, then
   Ramer-Douglas-Peucker down to at most 32 keys. The crop may jump only at a
   scene cut.

All coordinates are normalised to the *source* frame (0-1). Times are source
seconds. Outputs are plain dataclasses; a later package maps them to the
Timeline schema.
"""
from __future__ import annotations

import math
import re
import resource
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from api.services.media import run

LABEL = 'motion/edge-energy reframing; not face or subject detection'
LIMITATIONS = (
    'Follows the strongest motion and edge density, not a person: a busy background '
    '(crowd, foliage, scrolling UI, on-screen tickers) can pull the crop away from the speaker.',
    'Two people far apart: the window lands on whoever moves more, or holds on the previous '
    'position when neither dominates; it never splits the frame.',
    'Static talking heads with low motion rely on edges, which favour textured clothing or '
    'backgrounds over a smooth face.',
    'Camera pans and zooms produce motion everywhere; confidence gating then holds the previous centre.',
    'Burned-in subtitles and logos add edge energy; a lower-third caption biases the crop toward wide text.',
)

MODES = ('subject_follow', 'dynamic_center')
SAMPLE_WIDTH = 96
SAMPLE_FPS = 4
CONFIDENCE_MIN = 1.15
MEDIAN_K = 5
DEADZONE = 0.06
EMA_ALPHA = {'subject_follow': .25, 'dynamic_center': .12}
CENTER_RANGE = .15
MAX_VELOCITY = .22   # source widths per second
MAX_ACCEL = .6       # source widths per second squared
MAX_KEYS = 32
MOTION_NOISE = 12    # grey-level difference treated as compression noise
# Analysis-only decode shortcuts: skip non-reference frames (only 4 fps are kept
# anyway) and the deblocking filter (invisible at 96 px). Four decode threads
# keep total CPU near the single-thread cost while staying ~1.3 s wall for 60 s 1080p.
DECODE_FLAGS = ('-skip_frame:v', 'noref', '-skip_loop_filter:v', 'all', '-threads', '4')
_NOISE_TABLE = bytes(0 if v < MOTION_NOISE else v for v in range(256))


@dataclass
class CropKey:
    t: float          # source seconds
    cx: float         # crop-window centre, normalised source x
    cy: float         # crop-window centre, normalised source y
    cut: bool = False  # True: jump to this value (scene cut), do not interpolate from the previous key

    def as_tuple(self):
        return (self.t, self.cx, self.cy)


@dataclass
class EnergySamples:
    times: list            # source seconds per sampled frame
    profiles: list         # per frame, column energy over the safe-area columns (list of floats)
    col0: int              # first safe-area column in the 96 px grid
    grid_w: int            # 96
    grid_h: int
    safe_area: tuple       # normalised (x, y, w, h)
    src_w: int
    src_h: int
    timings: dict = field(default_factory=dict)


@dataclass
class ReframeResult:
    mode: str
    keys: list                 # list[CropKey], <= 32
    safe_area: tuple           # normalised (x, y, w, h) of the active picture
    crop_w: float              # crop window width, normalised to source width
    crop_h: float              # crop window height, normalised to source height
    label: str = LABEL
    samples: int = 0
    confident_ratio: float = 0.0
    segments: int = 1
    timings: dict = field(default_factory=dict)
    notes: list = field(default_factory=list)

    def key_tuples(self):
        return [k.as_tuple() for k in self.keys]

    def to_dict(self):
        data = asdict(self)
        data['keys'] = [asdict(k) for k in self.keys]
        return data


# --------------------------------------------------------------------------- sampling

def _probe_size(path):
    out, _ = run(['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries',
                  'stream=width,height', '-of', 'csv=p=0:s=x', path])
    w, h = out.strip().splitlines()[0].split('x')[:2]
    return int(w), int(h)


def _parse_cropdetect(stderr, scaled_w, scaled_h):
    """Last cropdetect line (accumulated bounds, reset=0) -> normalised safe area."""
    matches = re.findall(r'crop=(\d+):(\d+):(\d+):(\d+)', stderr)
    if not matches:
        return (0.0, 0.0, 1.0, 1.0)
    w, h, x, y = (int(v) for v in matches[-1])
    area = (w * h) / float(scaled_w * scaled_h)
    if w <= 0 or h <= 0 or area < .3:
        # Very dark footage can look like "all letterbox"; never trust a tiny area.
        return (0.0, 0.0, 1.0, 1.0)
    return (x / scaled_w, y / scaled_h, w / scaled_w, h / scaled_h)


def detect_safe_area(path, start, duration, sample_seconds=10.0):
    """Stand-alone cropdetect over up to ``sample_seconds`` centred in the range."""
    src_w, src_h = _probe_size(path)
    span = min(sample_seconds, max(.5, duration))
    a = max(0.0, start + (duration - span) / 2)
    cw, ch = _cropdetect_size(src_w, src_h)
    _, stderr = run(['ffmpeg', '-hide_banner', '-nostdin', '-ss', f'{a:.3f}', '-t', f'{span:.3f}', '-i', path,
                     '-an', '-sn', '-vf', f'fps=2,scale={cw}:{ch},cropdetect=limit=24:round=2:reset=0',
                     '-f', 'null', '-'])
    return _parse_cropdetect(stderr, cw, ch)


def _cropdetect_size(src_w, src_h):
    cw = min(src_w, 480)
    ch = max(2, int(round(src_h * cw / src_w / 2)) * 2)
    return cw - cw % 2, ch


def _grid_height(src_w, src_h):
    return max(2, int(round(SAMPLE_WIDTH * src_h / src_w / 2)) * 2)


def sample_energy(path, start, end, work_dir, src_w=None, src_h=None, safe_area=None, fps=SAMPLE_FPS):
    """One decode pass -> per-frame column energy profiles over the safe area.

    ``work_dir`` must exist and be writable; two small raw grey files are written
    there and removed afterwards.
    """
    t0, c0 = time.monotonic(), resource.getrusage(resource.RUSAGE_CHILDREN)
    if not src_w or not src_h:
        src_w, src_h = _probe_size(path)
    gw, gh = SAMPLE_WIDTH, _grid_height(src_w, src_h)
    cw, ch = _cropdetect_size(src_w, src_h)
    duration = max(.1, end - start)
    work = Path(work_dir)
    motion_path, edge_path = work / 'reframe_motion.gray', work / 'reframe_edges.gray'
    graph = (f'[0:v]fps={fps},split=2[cd][s];'
             f'[cd]scale={cw}:{ch},cropdetect=limit=24:round=2:reset=0[cdo];'
             f'[s]scale={gw}:{gh}:flags=area,format=gray,split=2[a][b];'
             f'[a]tblend=all_mode=difference[m];[b]edgedetect=low=0.1:high=0.3[e]')
    args = ['ffmpeg', '-hide_banner', '-nostdin', '-y', *DECODE_FLAGS, '-ss', f'{start:.3f}', '-t', f'{duration:.3f}',
            '-i', path, '-an', '-sn', '-filter_complex', graph,
            '-map', '[cdo]', '-f', 'null', '-',
            '-map', '[m]', '-f', 'rawvideo', '-pix_fmt', 'gray', motion_path,
            '-map', '[e]', '-f', 'rawvideo', '-pix_fmt', 'gray', edge_path]
    try:
        _, stderr = run(args)
        motion_raw, edge_raw = motion_path.read_bytes(), edge_path.read_bytes()
    finally:
        for p in (motion_path, edge_path):
            try:
                p.unlink()
            except FileNotFoundError:
                pass
    t1, c1 = time.monotonic(), resource.getrusage(resource.RUSAGE_CHILDREN)
    detected = _parse_cropdetect(stderr, cw, ch)
    safe = tuple(safe_area) if safe_area else detected
    frame = gw * gh
    edges = [memoryview(edge_raw)[i:i + frame] for i in range(0, len(edge_raw) - frame + 1, frame)]
    motions = [memoryview(motion_raw)[i:i + frame] for i in range(0, len(motion_raw) - frame + 1, frame)]

    sx, sy, sw, sh = safe
    col0, col1 = int(math.ceil(sx * gw - 1e-6)), int(math.floor((sx + sw) * gw + 1e-6))
    row0, row1 = int(math.ceil(sy * gh - 1e-6)), int(math.floor((sy + sh) * gh + 1e-6))
    # Skip the boundary pixel row/column of a letterbox/pillarbox: bar edges are not content.
    if sx > .005:
        col0 += 1
    if sx + sw < .995:
        col1 -= 1
    if sy > .005:
        row0 += 1
    if sy + sh < .995:
        row1 -= 1
    col0, col1 = max(0, col0), min(gw, max(col0 + 1, col1))
    row0, row1 = max(0, row0), min(gh, max(row0 + 1, row1))

    def columns(buf):
        b = bytes(buf)
        return [sum(b[row0 * gw + x:row1 * gw:gw]) for x in range(col0, col1)]

    profiles, times = [], []
    zero = [0] * (col1 - col0)
    for i, e in enumerate(edges):
        # tblend emits N-1 frames: motion[i-1] is the difference ending at edge frame i.
        mcols = columns(bytes(motions[i - 1]).translate(_NOISE_TABLE)) if 0 < i <= len(motions) else zero
        ecols = columns(e)
        mmax, emax = max(mcols) if mcols else 0, max(ecols) if ecols else 0
        profile = [(.7 * m / mmax if mmax else 0.0) + (.3 * v / emax if emax else 0.0)
                   for m, v in zip(mcols, ecols)]
        profiles.append(profile)
        times.append(start + i / float(fps))
    t2 = time.monotonic()
    timings = {'ffmpeg_wall_s': round(t1 - t0, 3),
               'ffmpeg_cpu_s': round((c1.ru_utime + c1.ru_stime) - (c0.ru_utime + c0.ru_stime), 3),
               'python_s': round(t2 - t1, 3)}
    return EnergySamples(times, profiles, col0, gw, gh, safe, src_w, src_h, timings)


# --------------------------------------------------------------------------- per-frame centres

def crop_width(safe_area, src_w, src_h, aspect=9 / 16):
    """Normalised width of a full-safe-height ``aspect`` crop window."""
    return min(safe_area[2], safe_area[3] * src_h * aspect / src_w)


def best_window(profile, k):
    """Return (centre_offset_in_columns, confidence) of the best width-``k`` window.

    Ties (a small subject inside a wide window gives a plateau of equal sums) are
    broken by taking the middle of the plateau containing the peak, not its left edge.
    """
    n = len(profile)
    if n == 0:
        return 0.0, 0.0
    k = max(1, min(n, k))
    run_sum = sum(profile[:k])
    sums = [run_sum]
    for j in range(k, n):
        run_sum += profile[j] - profile[j - k]
        sums.append(run_sum)
    peak = max(sums)
    mean = sum(sums) / len(sums)
    if peak <= 0 or mean <= 0:
        return n / 2.0, 0.0
    j = sums.index(peak)
    lo = hi = j
    tol = peak * .97
    while lo > 0 and sums[lo - 1] >= tol:
        lo -= 1
    while hi < len(sums) - 1 and sums[hi + 1] >= tol:
        hi += 1
    return (lo + hi) / 2.0 + k / 2.0, peak / mean


def frame_centres(samples, crop_w=None):
    """Per frame (t, cx, confidence) in normalised source x."""
    if crop_w is None:
        crop_w = crop_width(samples.safe_area, samples.src_w, samples.src_h)
    k = int(round(crop_w * samples.grid_w))
    out = []
    for t, profile in zip(samples.times, samples.profiles):
        offset, conf = best_window(profile, k)
        out.append((t, (samples.col0 + offset) / samples.grid_w, conf))
    return out


# --------------------------------------------------------------------------- smoothing primitives

def median_filter(xs, k=MEDIAN_K):
    half = k // 2
    out = []
    for i in range(len(xs)):
        window = sorted(xs[max(0, i - half):i + half + 1])
        m = len(window)
        out.append(window[m // 2] if m % 2 else (window[m // 2 - 1] + window[m // 2]) / 2)
    return out


def deadzone(xs, dz=DEADZONE):
    """Hold the value until the target leaves a +-dz band around it."""
    if not xs:
        return []
    held, out = xs[0], []
    for x in xs:
        if abs(x - held) >= dz:
            held = x
        out.append(held)
    return out


def ema_zero_phase(xs, alpha):
    """Forward then backward EMA: smooth without the lag of a one-pass EMA."""
    if not xs:
        return []
    fwd, y = [], xs[0]
    for x in xs:
        y += alpha * (x - y)
        fwd.append(y)
    out, y = [0.0] * len(fwd), fwd[-1]
    for i in range(len(fwd) - 1, -1, -1):
        y += alpha * (fwd[i] - y)
        out[i] = y
    return out


def clamp_motion(times, xs, vmax=MAX_VELOCITY, amax=MAX_ACCEL):
    """Velocity/acceleration-limited follower with braking, so it never overshoots."""
    if not xs:
        return []
    y, v, out = xs[0], 0.0, [xs[0]]
    for i in range(1, len(xs)):
        dt = max(1e-3, times[i] - times[i - 1])
        d = xs[i] - y
        target = math.copysign(min(vmax, abs(d) / dt, math.sqrt(2 * amax * abs(d))), d) if d else 0.0
        v = max(v - amax * dt, min(v + amax * dt, target))
        if (d > 0 and v * dt > d) or (d < 0 and v * dt < d):
            v = d / dt
        y += v * dt
        out.append(y)
    return out


def rdp(points, epsilon):
    """Ramer-Douglas-Peucker on (t, x) using vertical (value) distance."""
    if len(points) < 3:
        return list(points)
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        a, b = stack.pop()
        (ta, xa), (tb, xb) = points[a][:2], points[b][:2]
        best, idx = -1.0, -1
        for i in range(a + 1, b):
            t, x = points[i][:2]
            interp = xa + (xb - xa) * ((t - ta) / (tb - ta) if tb != ta else 0)
            dist = abs(x - interp)
            if dist > best:
                best, idx = dist, i
        if idx > 0 and best > epsilon:
            keep[idx] = True
            stack.extend([(a, idx), (idx, b)])
    return [p for p, k in zip(points, keep) if k]


def simplify_segments(segments, max_keys=MAX_KEYS):
    """RDP each segment with one shared epsilon, growing it until the total fits.

    ``segments`` is a list of dense [(t, x), ...]. Returns a list of per-segment
    simplified lists. When even two keys per segment do not fit, segments get
    one constant key each, and the shortest segments merge into a neighbour.
    """
    segments = [s for s in segments if s]
    if not segments:
        return []
    eps = .001
    while True:
        out = [rdp(s, eps) for s in segments]
        if sum(len(s) for s in out) <= max_keys:
            return out
        if all(len(s) <= 2 for s in out) or eps > 1:
            break
        eps *= 1.5
    # Too many segments: constant crop per segment (median), merge shortest.
    flat = [[(s[0][0], sorted(p[1] for p in s)[len(s) // 2]), s[-1][0]] for s in segments]
    while len(flat) > max_keys:
        i = min(range(len(flat)), key=lambda j: flat[j][1] - flat[j][0][0])
        j = i - 1 if i > 0 else i + 1
        a, b = sorted((i, j))
        flat[a] = [flat[a][0], flat[b][1]]
        del flat[b]
    return [[f[0]] for f in flat]


def segment_indices(times, cuts):
    """Split frame indices at scene cuts -> list of (lo, hi_exclusive, cut_time_or_None)."""
    if not times:
        return []
    bounds, cut_times = [0], [None]
    for c in sorted(set(cuts or ())):
        i = next((j for j, t in enumerate(times) if t >= c), None)
        if i is not None and i > bounds[-1]:
            bounds.append(i)
            cut_times.append(float(c))
    bounds.append(len(times))
    return [(bounds[i], bounds[i + 1], cut_times[i]) for i in range(len(bounds) - 1) if bounds[i + 1] > bounds[i]]


def gate_confidence(centres, default, threshold=CONFIDENCE_MIN):
    """Replace low-confidence centres by the previous confident one.

    At the start of a segment there is no previous value, so leading uncertain
    frames take the *first confident* value of the segment (back-fill), or
    ``default`` (frame centre) when no frame in the segment is confident.
    """
    first = next((c for _, c, conf in centres if conf >= threshold), default)
    held, out = first, []
    for _, c, conf in centres:
        if conf >= threshold:
            held = c
        out.append(held)
    return out


def smooth_path(centres, cuts=(), mode='subject_follow', bounds=(0.0, 1.0), default=.5):
    """Gated, filtered, clamped dense path per segment.

    ``centres`` is [(t, cx, confidence)]. Returns a list of segments, each a dense
    list of (t, cx), and the first-sample time of each segment's cut (or None).
    """
    if mode not in MODES:
        raise ValueError(f'unknown reframe mode {mode!r}')
    lo, hi = bounds
    if mode == 'dynamic_center':
        lo, hi = max(lo, default - CENTER_RANGE), min(hi, default + CENTER_RANGE)
    times = [c[0] for c in centres]
    segs, cut_times = [], []
    for a, b, cut in segment_indices(times, cuts):
        part = centres[a:b]
        ts = [p[0] for p in part]
        xs = gate_confidence(part, default)
        xs = [min(hi, max(lo, x)) for x in xs] if hi >= lo else [default] * len(xs)
        xs = median_filter(xs, MEDIAN_K)
        xs = deadzone(xs, DEADZONE)
        xs = ema_zero_phase(xs, EMA_ALPHA[mode])
        xs = clamp_motion(ts, xs, MAX_VELOCITY, MAX_ACCEL)
        xs = [min(hi, max(lo, x)) for x in xs] if hi >= lo else xs
        segs.append(list(zip(ts, xs)))
        cut_times.append(cut)
    return segs, cut_times


# --------------------------------------------------------------------------- top level

def path_to_keys(segs, cut_times, cy, start=None, max_keys=MAX_KEYS):
    simplified = simplify_segments(segs, max_keys)
    keys = []
    # simplify_segments may merge segments; recover cut flags by position.
    merged = len(simplified) != len(segs)
    for i, seg in enumerate(simplified):
        for j, (t, x) in enumerate(seg):
            is_first = j == 0
            if is_first and i == 0 and start is not None:
                t = min(t, start)
            elif is_first and not merged and cut_times[i] is not None:
                t = cut_times[i]
            keys.append(CropKey(round(t, 3), round(x, 4), round(cy, 4), cut=is_first and i > 0))
    return keys


def reframe(path, start, end, work_dir, mode='subject_follow', scene_boundaries=(),
            src_w=None, src_h=None, safe_area=None, max_keys=MAX_KEYS):
    """Sample, choose and smooth a 9:16 crop path for ``[start, end)`` of ``path``.

    ``scene_boundaries`` are source seconds (e.g. ``sample_signals(...)['scene_boundaries']``
    or scene_score > .4). Cuts outside the range are ignored.
    """
    if mode not in MODES:
        raise ValueError(f'unknown reframe mode {mode!r}')
    samples = sample_energy(path, start, end, work_dir, src_w, src_h, safe_area)
    t0 = time.monotonic()
    safe = samples.safe_area
    cw = crop_width(safe, samples.src_w, samples.src_h)
    cy = safe[1] + safe[3] / 2
    centre = safe[0] + safe[2] / 2
    notes = []
    if cw >= safe[2] - 1e-3 or not samples.times:
        notes.append('Source is already as narrow as 9:16 (or no frames sampled); centred crop.')
        return ReframeResult(mode, [CropKey(round(start, 3), round(centre, 4), round(cy, 4))], safe, cw, safe[3],
                             samples=len(samples.times), timings=samples.timings, notes=notes)
    centres = frame_centres(samples, cw)
    bounds = (safe[0] + cw / 2, safe[0] + safe[2] - cw / 2)
    cuts = [c for c in (scene_boundaries or ()) if start < c < end]
    segs, cut_times = smooth_path(centres, cuts, mode, bounds, centre)
    keys = path_to_keys(segs, cut_times, cy, start, max_keys)
    confident = sum(1 for c in centres if c[2] >= CONFIDENCE_MIN) / len(centres)
    if confident < .5:
        notes.append('Under half of the frames had a clear motion/edge peak; the crop mostly holds position.')
    timings = dict(samples.timings, smoothing_s=round(time.monotonic() - t0, 3))
    return ReframeResult(mode, keys, safe, cw, safe[3], samples=len(centres), confident_ratio=round(confident, 3),
                         segments=len(segs), timings=timings, notes=notes)


# --------------------------------------------------------------------------- split screen

SPLIT_CORNERS = ('top-left', 'top-right', 'bottom-left', 'bottom-right')


def _fit_aspect(region, aspect, src_w, src_h):
    """Trim the longer side of a normalised region around its centre to ``aspect`` (w/h in pixels)."""
    x, y, w, h = region
    pw, ph = w * src_w, h * src_h
    if pw / ph > aspect:
        nw = ph * aspect / src_w
        x, w = x + (w - nw) / 2, nw
    else:
        nh = pw / aspect / src_h
        y, h = y + (h - nh) / 2, nh
    return (round(x, 4), round(y, 4), round(w, 4), round(h, 4))


def split_screen_regions(src_w, src_h, corner='top-left', override=None, face_panel=.35,
                         facecam_size=.28, out_w=1080, out_h=1920, safe_area=(0.0, 0.0, 1.0, 1.0)):
    """Fixed facecam + gameplay source regions for a stacked 9:16 split screen.

    The facecam comes from ``corner`` (a preset box ``facecam_size`` of the safe
    width) unless the user passes ``override`` = normalised (x, y, w, h). Both
    regions are trimmed (never stretched) to the aspect of their output panel:
    facecam on top, ``face_panel`` of the output height; gameplay below, full
    safe height, centred horizontally.
    """
    if corner not in SPLIT_CORNERS:
        raise ValueError(f'corner must be one of {SPLIT_CORNERS}')
    if not 0.15 <= face_panel <= .6:
        raise ValueError('face_panel must be between 0.15 and 0.6')
    sx, sy, sw, sh = safe_area
    face_aspect = out_w / (out_h * face_panel)
    game_aspect = out_w / (out_h * (1 - face_panel))
    if override is not None:
        x, y, w, h = (float(v) for v in override)
        if not all(math.isfinite(v) for v in (x, y, w, h)) or w <= .02 or h <= .02:
            raise ValueError('facecam override must be a finite normalised (x, y, w, h) with w, h > 0.02')
        x, y = min(max(0.0, x), .98), min(max(0.0, y), .98)
        w, h = min(w, 1 - x), min(h, 1 - y)
        source = 'user'
    else:
        w = sw * facecam_size
        h = w * src_w / face_aspect / src_h
        if h > sh:
            h = sh
            w = h * src_h * face_aspect / src_w
        x = sx if 'left' in corner else sx + sw - w
        y = sy if corner.startswith('top') else sy + sh - h
        source = 'preset'
    facecam = _fit_aspect((x, y, w, h), face_aspect, src_w, src_h)
    gw = min(sw, sh * src_h * game_aspect / src_w)
    gameplay = _fit_aspect((sx + (sw - gw) / 2, sy, gw, sh), game_aspect, src_w, src_h)
    return {'facecam': facecam, 'gameplay': gameplay, 'face_panel': face_panel,
            'corner': corner if source == 'preset' else None, 'source': source,
            'label': 'fixed split-screen regions from a preset corner or user box; no face detection'}
