"""WP4: Timeline schema, FFmpeg graph compiler, render and QA (synthetic lavfi media only)."""
import json
import re
import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings
from pydantic import ValidationError

from .editing import ffmpeg_graph, qa
from .editing.ffmpeg_graph import Capabilities, compile as compile_timeline, piecewise
from .editing.render import render_timeline
from .editing.timeline import Timeline
from .services.media import StudioError, probe, run
from .services.rendering import render_command, validate_output

W, H, FPS = 180, 320, 25
FRAME = 1 / FPS
CAPS = Capabilities('test', frozenset({'overlay', 'zoompan', 'xfade', 'sidechaincompress'}), True)


def media(path='src.mp4', duration=8.0, audio=True, license='owned', **kw):
    return dict(kind='source', path=path, duration=duration, has_audio=audio, width=320, height=180,
                license_status=license, **kw)


def clip(cid, src_in, src_out, start, crop=None, **kw):
    return dict(id=cid, media=kw.pop('media', media()), src_in=src_in, src_out=src_out, start=start,
                crop=crop or {'mode': 'center'}, **kw)


def full_slot(name='main', z=0):
    return dict(name=name, rect=dict(x=0, y=0, w=W, h=H), z=z)


def timeline(clips, duration, layout=None, tracks=None, **kw):
    data = dict(canvas=dict(width=W, height=H, fps=FPS),
                layout=layout or dict(kind='single', slots=[full_slot()]),
                video=tracks or [dict(slot='main', clips=clips)], duration=duration, **kw)
    return Timeline.model_validate(data)


def frame(path, t, size=None):
    """Decode one RGB frame with FFmpeg into a Pillow image."""
    from PIL import Image
    out = Path(path).with_name(f'{Path(path).stem}_{t:.2f}.png')
    run(['ffmpeg', '-v', 'error', '-nostdin', '-y', '-ss', f'{t:.3f}', '-i', path, '-frames:v', '1', out])
    img = Image.open(out).convert('RGB')
    return img.resize(size) if size else img


def mean_diff(a, b):
    from PIL import ImageChops, ImageStat
    return sum(ImageStat.Stat(ImageChops.difference(a, b)).mean) / 3


def band_level(path, start, length, freq):
    _, err = run(['ffmpeg', '-hide_banner', '-nostdin', '-ss', str(start), '-t', str(length), '-i', path,
                  '-af', f'bandpass=f={freq}:width_type=h:w=60,volumedetect', '-f', 'null', '-'])
    return float(re.search(r'mean_volume: (-?[\d.]+) dB', err)[1])


@override_settings(PROCESS_TIMEOUT=120, MAX_SOURCE_DURATION=1800)
class TimelineValidationTests(SimpleTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings_override = override_settings(MEDIA_ROOT=Path(self.tmp.name))
        self.settings_override.enable()

    def tearDown(self):
        self.settings_override.disable()
        self.tmp.cleanup()

    def assertRejects(self, fragment, build):
        with self.assertRaises(ValidationError) as ctx:
            build()
        self.assertIn(fragment, str(ctx.exception))

    def test_valid_timeline_derives_provenance(self):
        tl = timeline([clip('a', 0, 2, 0), clip('b', 3, 5, 2)], 4)
        self.assertEqual(tl.provenance, [{'kind': 'source', 'path': 'src.mp4', 'asset_id': None, 'license_status': 'owned'}])
        self.assertTrue(tl.video[0].clips[0].media.abs_path.endswith('src.mp4'))

    def test_rejections(self):
        self.assertRejects('overlap', lambda: timeline([clip('a', 0, 2, 0), clip('b', 3, 5, 1.5)], 3.5))
        self.assertRejects('sorted', lambda: timeline([clip('a', 0, 2, 2), clip('b', 3, 5, 0)], 4))
        self.assertRejects('exceeds media duration', lambda: timeline([clip('a', 7, 9, 0)], 2))
        self.assertRejects('src_out must be greater', lambda: timeline([clip('a', 2, 2, 0)], 1))
        self.assertRejects('escapes MEDIA_ROOT', lambda: timeline([clip('a', 0, 2, 0, media=media('../outside.mp4'))], 2))
        self.assertRejects('escapes MEDIA_ROOT', lambda: timeline([clip('a', 0, 2, 0, media=media('/etc/hosts'))], 2))
        self.assertRejects('does not match', lambda: timeline([clip('a', 0, 2, 0)], 3))
        self.assertRejects('gap', lambda: timeline([clip('a', 0, 1, 0), clip('b', 2, 3, 1.5)], 2.5))
        self.assertRejects('fixed crop requires a region', lambda: timeline([clip('a', 0, 2, 0, crop={'mode': 'fixed'})], 2))
        keys = [dict(t=i * .05, cx=.5, cy=.5) for i in range(33)]
        self.assertRejects('at most 32', lambda: timeline([clip('a', 0, 2, 0, crop={'mode': 'path', 'keys': keys})], 2))
        self.assertRejects('greater than or equal to 1', lambda: timeline([clip('a', 0, 2, 0, zoom=[dict(t=1, zoom=0.5)])], 2))
        overlay = dict(id='o', kind='image', png='card.png', start=0, end=1, rect=dict(x=100, y=0, w=100, h=50))
        self.assertRejects('outside the canvas', lambda: timeline([clip('a', 0, 2, 0)], 2, overlays=[overlay]))
        many = [dict(overlay, id=f'o{i}', rect=dict(x=0, y=0, w=50, h=50)) for i in range(25)]
        self.assertRejects('at most 24', lambda: timeline([clip('a', 0, 2, 0)], 2, overlays=many))
        self.assertRejects('escapes MEDIA_ROOT', lambda: timeline([clip('a', 0, 2, 0)], 2,
            overlays=[dict(overlay, png='../../x.png', rect=dict(x=0, y=0, w=50, h=50))]))
        self.assertRejects('unknown slot', lambda: timeline([], 2, tracks=[dict(slot='nope', clips=[clip('a', 0, 2, 0)])]))
        self.assertRejects('less than or equal to 180', lambda: timeline([clip('a', 0, 200, 0, media=media(duration=300))], 200))
        silent = media(audio=False)
        self.assertRejects('no audio stream', lambda: timeline([clip('a', 0, 2, 0)], 2, audio=[
            dict(id='s', media=silent, src_in=0, src_out=2, start=0, role='source')]))

    def test_symlink_escape_rejected(self):
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        (Path(outside.name) / 'secret.mp4').write_bytes(b'x')
        (Path(self.tmp.name) / 'link.mp4').symlink_to(Path(outside.name) / 'secret.mp4')
        self.assertRejects('escapes MEDIA_ROOT', lambda: timeline([clip('a', 0, 2, 0, media=media('link.mp4'))], 2))

    def test_pip_track_may_have_gaps(self):
        layout = dict(kind='pip', slots=[full_slot(), dict(name='pip', rect=dict(x=100, y=200, w=60, h=100), z=1)])
        tl = timeline(None, 4, layout=layout, tracks=[dict(slot='main', clips=[clip('m', 0, 4, 0)]),
                                                      dict(slot='pip', clips=[clip('p', 0, 1, 1), clip('q', 2, 3, 2.5)])])
        self.assertEqual(len(tl.video), 2)


@override_settings(PROCESS_TIMEOUT=120, MAX_SOURCE_DURATION=1800)
class CompilerGoldenTests(SimpleTestCase):
    """Golden filter scripts for the key graph shapes (no FFmpeg execution)."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings_override = override_settings(MEDIA_ROOT=Path(self.tmp.name))
        self.settings_override.enable()

    def tearDown(self):
        self.settings_override.disable()
        self.tmp.cleanup()

    def plan(self, tl, **kw):
        return compile_timeline(tl, Path(self.tmp.name), capabilities=CAPS, **kw)

    def test_piecewise_expression(self):
        self.assertEqual(piecewise([(0, .2)]), '0.2')
        self.assertEqual(piecewise([(0, 0), (2, 1)]), 'if(lt(t,2),0+1*clip((t-0)/2,0,1),1)')
        self.assertEqual(piecewise([(0, 1), (1, 1.2)], 'it', ['linear', 'step']), 'if(lt(it,1),1,1.2)')

    def test_golden_three_segment_concat_with_audio(self):
        clips = [clip('a', 0, 1.5, 0), clip('b', 2.5, 4, 1.5), clip('c', 5, 6.5, 3)]
        audio = [dict(id=f's{i}', media=media(), src_in=c['src_in'], src_out=c['src_out'], start=c['start'],
                      role='source') for i, c in enumerate(clips)]
        plan = self.plan(timeline(clips, 4.5, audio=audio))
        self.assertEqual(plan.filter_script, GOLDEN_CONCAT)
        self.assertIn('-/filter_complex', plan.argv)
        self.assertTrue(Path(plan.argv[plan.argv.index('-/filter_complex') + 1]).is_file())
        self.assertEqual(plan.argv[plan.argv.index('-ss') + 1], '0.000')

    def test_golden_path_crop_and_cut_zoom(self):
        keys = [dict(t=0, cx=0, cy=.5), dict(t=2, cx=1, cy=.5), dict(t=4, cx=.5, cy=.5)]
        tl = timeline([clip('a', 1, 5, 0, crop={'mode': 'path', 'keys': keys}, zoom=[dict(t=2, zoom=1.25)], grade='none')], 4)
        self.assertEqual(self.plan(tl).filter_script, GOLDEN_PATH)

    def test_golden_split_and_ducking(self):
        layout = dict(kind='split_v', slots=[dict(name='top', rect=dict(x=0, y=0, w=W, h=192)),
                                             dict(name='bottom', rect=dict(x=0, y=192, w=W, h=128))])
        tracks = [dict(slot='top', clips=[clip('t', 0, 4, 0, crop={'mode': 'fixed', 'region': [0, 0, .7, 1]})]),
                  dict(slot='bottom', clips=[clip('b', 0, 4, 0, crop={'mode': 'fixed', 'region': [.7, 0, .3, .5]})])]
        audio = [dict(id='m', media=media('music.m4a', 8, license='licensed'), src_in=0, src_out=4, start=0,
                      role='music', gain_db=-6, duck_under=['narration']),
                 dict(id='n', media=media('voice.m4a', 4, license='generated'), src_in=0, src_out=2, start=1,
                      role='narration')]
        plan = self.plan(timeline(None, 4, layout=layout, tracks=tracks, audio=audio))
        self.assertEqual(plan.filter_script, GOLDEN_SPLIT_DUCK)
        self.assertEqual(plan.argv.count('-i'), 3)

    def test_inline_fallback_when_script_unsupported(self):
        tl = timeline([clip('a', 0, 2, 0)], 2)
        caps = Capabilities('old', frozenset(), False)
        plan = compile_timeline(tl, Path(self.tmp.name), capabilities=caps)
        self.assertIn('-filter_complex', plan.argv)
        self.assertNotIn('-/filter_complex', plan.argv)
        self.assertIsNone(plan.script_path)

    def test_caption_renderer_selection(self):
        safe = dict(x=10, y=200, w=160, h=100)
        cues = [dict(start=.2, end=1.5, text='hello')]
        tl = timeline([clip('a', 0, 2, 0)], 2, captions=dict(cues=cues, safe_rect=safe))
        no_ass = self.plan(tl)
        self.assertEqual(no_ass.caption_renderer, 'png')
        self.assertTrue(any('not burned in' in w for w in no_ass.warnings))
        with_ass = compile_timeline(tl, Path(self.tmp.name), capabilities=Capabilities('x', frozenset({'ass'}), True))
        self.assertEqual(with_ass.caption_renderer, 'ass')
        self.assertRegex(with_ass.filter_script, r'ass=captions_[0-9a-f]{10}\.ass\[out\]')
        self.assertEqual(with_ass.cwd, str(Path(self.tmp.name)))
        forced = timeline([clip('a', 0, 2, 0)], 2, captions=dict(cues=cues, safe_rect=safe, renderer='ass'))
        with self.assertRaises(StudioError):
            self.plan(forced)

    def test_hostile_overlay_path_stays_one_argv_element(self):
        name = "card';[x]null[y];.png"
        tl = timeline([clip('a', 0, 2, 0)], 2, overlays=[dict(id='o', kind='image', png=name, start=0, end=1,
                                                             rect=dict(x=0, y=0, w=50, h=50))])
        plan = self.plan(tl, inline=True)
        self.assertIn(str(Path(self.tmp.name) / name), plan.argv)
        self.assertNotIn(name, plan.filter_script)


GOLDEN_CONCAT = (
    '[0:v]trim=start=0:end=1.5,setpts=PTS-STARTPTS,scale=180:320:force_original_aspect_ratio=increase,crop=180:320,setsar=1,eq=contrast=1.015:saturation=1.03,fps=25,format=yuv420p[p1];\n'
    '[0:v]trim=start=2.5:end=4,setpts=PTS-STARTPTS,scale=180:320:force_original_aspect_ratio=increase,crop=180:320,setsar=1,eq=contrast=1.015:saturation=1.03,fps=25,format=yuv420p[p2];\n'
    '[0:v]trim=start=5:end=6.5,setpts=PTS-STARTPTS,scale=180:320:force_original_aspect_ratio=increase,crop=180:320,setsar=1,eq=contrast=1.015:saturation=1.03,fps=25,format=yuv420p[p3];\n'
    '[p1][p2][p3]concat=n=3:v=1:a=0,fps=25[c4];\n'
    '[c4]null[out];\n'
    '[0:a]atrim=start=0:end=1.5,asetpts=PTS-STARTPTS,aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,afade=t=in:st=0:d=0.015,afade=t=out:st=1.485:d=0.015[a5];\n'
    '[0:a]atrim=start=2.5:end=4,asetpts=PTS-STARTPTS,aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,afade=t=in:st=0:d=0.015,afade=t=out:st=1.485:d=0.015,adelay=delays=72000S:all=1[a6];\n'
    '[0:a]atrim=start=5:end=6.5,asetpts=PTS-STARTPTS,aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,afade=t=in:st=0:d=0.015,afade=t=out:st=1.485:d=0.015,adelay=delays=144000S:all=1[a7];\n'
    '[a5][a6][a7]amix=inputs=3:normalize=0:duration=longest,apad=whole_dur=4.5[source_8];\n'
    '[source_8]loudnorm=I=-16:TP=-1.5:LRA=11,alimiter=limit=0.95,aresample=48000,atrim=end=4.5[aout]\n'
)
GOLDEN_PATH = (
    "[0:v]trim=start=0:end=2,setpts=PTS-STARTPTS,crop=w='min(iw,ih*0.5625)':h='min(ih,iw/0.5625)':x='clip((if(lt(t,2),0+1*clip((t-0)/2,0,1),if(lt(t,4),1-0.5*clip((t-2)/2,0,1),0.5)))*iw-ow/2,0,iw-ow)':y='clip((if(lt(t,2),0.5,if(lt(t,4),0.5,0.5)))*ih-oh/2,0,ih-oh)',scale=180:320,setsar=1,fps=25,format=yuv420p[p1];\n"
    "[0:v]trim=start=2:end=4,setpts=PTS-STARTPTS,crop=w='min(iw,ih*0.5625)/1.25':h='min(ih,iw/0.5625)/1.25':x='clip((if(lt((t+2),2),0+1*clip(((t+2)-0)/2,0,1),if(lt((t+2),4),1-0.5*clip(((t+2)-2)/2,0,1),0.5)))*iw-ow/2,0,iw-ow)':y='clip((if(lt((t+2),2),0.5,if(lt((t+2),4),0.5,0.5)))*ih-oh/2,0,ih-oh)',scale=180:320,setsar=1,fps=25,format=yuv420p[p2];\n"
    '[p1][p2]concat=n=2:v=1:a=0,fps=25[c3];\n'
    '[c3]null[out]\n'
)
GOLDEN_SPLIT_DUCK = (
    'color=c=black:s=180x320:r=25:d=4,setsar=1[base1];\n'
    "[0:v]trim=start=0:end=4,setpts=PTS-STARTPTS,crop=w='iw*0.7':h='ih*1':x='iw*0':y='ih*0',scale=180:192:force_original_aspect_ratio=increase,crop=180:192,setsar=1,eq=contrast=1.015:saturation=1.03[p2];\n"
    '[base1][p2]overlay=x=0:y=0:eof_action=pass[v3];\n'
    "[0:v]trim=start=0:end=4,setpts=PTS-STARTPTS,crop=w='iw*0.3':h='ih*0.5':x='iw*0.7':y='ih*0',scale=180:128:force_original_aspect_ratio=increase,crop=180:128,setsar=1,eq=contrast=1.015:saturation=1.03[p4];\n"
    '[v3][p4]overlay=x=0:y=192:eof_action=pass[v5];\n'
    '[v5]null[out];\n'
    '[1:a]atrim=start=0:end=4,asetpts=PTS-STARTPTS,aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,afade=t=in:st=0:d=0.015,afade=t=out:st=3.985:d=0.015,volume=-6dB[a6];\n'
    '[2:a]atrim=start=0:end=2,asetpts=PTS-STARTPTS,aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,afade=t=in:st=0:d=0.015,afade=t=out:st=1.985:d=0.015,adelay=delays=48000S:all=1[a7];\n'
    '[a6]apad=whole_dur=4[music_8];\n'
    '[a7]apad=whole_dur=4[narration_9];\n'
    '[narration_9]asplit=2[narration_c10][narration_c11];\n'
    '[music_8][narration_c11]sidechaincompress=threshold=0.02:ratio=10:attack=15:release=350:makeup=1[music_d12];\n'
    '[music_d12][narration_c10]amix=inputs=2:normalize=0:duration=longest,loudnorm=I=-16:TP=-1.5:LRA=11,alimiter=limit=0.95,aresample=48000,atrim=end=4[aout]\n'
)


@override_settings(PROCESS_TIMEOUT=120, MAX_SOURCE_DURATION=1800)
class RenderTests(SimpleTestCase):
    """Real encodes at 180x320 on the local (libass-less) FFmpeg."""
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if not shutil.which('ffmpeg'):
            raise cls.failureException('ffmpeg required')
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        cls.override = override_settings(MEDIA_ROOT=cls.root)
        cls.override.enable()
        run(['ffmpeg', '-nostdin', '-y', '-f', 'lavfi', '-i', 'testsrc2=size=320x180:rate=25', '-f', 'lavfi',
             '-i', 'sine=frequency=440:sample_rate=48000', '-t', '8', '-c:v', 'libx264', '-preset', 'ultrafast',
             '-g', '25', '-c:a', 'aac', cls.root / 'src.mp4'])
        run(['ffmpeg', '-nostdin', '-y', '-f', 'lavfi', '-i', 'sine=frequency=220:sample_rate=48000', '-t', '8',
             '-c:a', 'aac', cls.root / 'music.m4a'])
        run(['ffmpeg', '-nostdin', '-y', '-f', 'lavfi', '-i', 'sine=frequency=1760:sample_rate=48000', '-t', '4',
             '-af', 'volume=6dB', '-c:a', 'aac', cls.root / 'voice.m4a'])
        from PIL import Image
        Image.new('RGBA', (100, 60), (255, 0, 0, 255)).save(cls.root / 'card.png')
        Image.new('RGBA', (160, 40), (255, 255, 0, 255)).save(cls.root / 'cap.png')

    @classmethod
    def tearDownClass(cls):
        cls.override.disable()
        cls.tmp.cleanup()
        super().tearDownClass()

    def render(self, tl, name):
        result = render_timeline(tl, self.root / f'{name}.mp4', self.root / f'work_{name}', x264_preset='ultrafast')
        self.assertTrue(all(result.report['checks'].values()), result.report)
        self.assertEqual(result.attempts, 1, result.warnings)
        return result

    def video_duration(self, path):
        return qa._stream_durations(path)['video']

    def test_three_segment_concat_removes_dead_air(self):
        clips = [clip('a', 0, 1.5, 0), clip('b', 2.5, 4, 1.5), clip('c', 5, 6.5, 3)]
        audio = [dict(id=f's{i}', media=media(), src_in=c['src_in'], src_out=c['src_out'], start=c['start'],
                      role='source') for i, c in enumerate(clips)]
        result = self.render(timeline(clips, 4.5, audio=audio), 'concat')
        self.assertLessEqual(abs(self.video_duration(result.output) - 4.5), FRAME + 1e-3)
        self.assertTrue(result.report['extended_checks']['av_drift'], result.report['stream_durations'])
        # Output 2.0s is source 3.0s (second segment), not source 2.0s (removed dead air).
        out = frame(result.output, 2.0, (W, H))
        src = self.root / 'src.mp4'
        expect = frame(src, 3.0).crop((70, 0, 171, 180)).resize((W, H))
        removed = frame(src, 2.0).crop((70, 0, 171, 180)).resize((W, H))
        self.assertLess(mean_diff(out, expect), mean_diff(out, removed))

    def test_crossfade_keeps_timeline_duration(self):
        clips = [clip('a', 0, 2, 0), clip('b', 4, 6, 2, transition_in=dict(kind='crossfade', duration=.4))]
        result = self.render(timeline(clips, 4), 'xfade')
        self.assertIn('xfade=transition=fade:duration=0.4:offset=2', result.plan.filter_script)
        self.assertLessEqual(abs(self.video_duration(result.output) - 4), FRAME + 1e-3)

    def test_cut_zoom_and_eased_zoom(self):
        cut = self.render(timeline([clip('a', 0, 4, 0, zoom=[dict(t=2, zoom=1.5)])], 4), 'cutzoom')
        self.assertEqual(cut.plan.filter_script.count('trim='), 2)
        self.assertLessEqual(abs(self.video_duration(cut.output) - 4), FRAME + 1e-3)
        before, after = frame(cut.output, 1.9, (W, H)), frame(cut.output, 2.1, (W, H))
        self.assertGreater(mean_diff(before, after), 3)   # visible punch-in at the cut
        ease = self.render(timeline([clip('a', 0, 4, 0, zoom=[dict(t=0, zoom=1, mode='ease'),
                                                              dict(t=4, zoom=1.4, mode='ease')])], 4), 'easezoom')
        self.assertIn('zoompan=', ease.plan.filter_script)
        self.assertLessEqual(abs(self.video_duration(ease.output) - 4), FRAME + 1e-3)

    def test_path_crop_follows_keys(self):
        keys = [dict(t=0, cx=0, cy=.5), dict(t=3.5, cx=1, cy=.5)]
        result = self.render(timeline([clip('a', 0, 4, 0, crop={'mode': 'path', 'keys': keys})], 4), 'path')
        src = self.root / 'src.mp4'
        start = frame(result.output, 0.0, (W, H))
        left, right = frame(src, 0.0).crop((0, 0, 101, 180)).resize((W, H)), frame(src, 0.0).crop((219, 0, 320, 180)).resize((W, H))
        self.assertLess(mean_diff(start, left), mean_diff(start, right))
        end = frame(result.output, 3.8, (W, H))
        left, right = frame(src, 3.8).crop((0, 0, 101, 180)).resize((W, H)), frame(src, 3.8).crop((219, 0, 320, 180)).resize((W, H))
        self.assertLess(mean_diff(end, right), mean_diff(end, left))

    def test_split_v_pip_and_letterbox_layouts(self):
        split = dict(kind='split_v', slots=[dict(name='top', rect=dict(x=0, y=0, w=W, h=192)),
                                            dict(name='bottom', rect=dict(x=0, y=192, w=W, h=128))])
        tracks = [dict(slot='top', clips=[clip('t', 0, 3, 0, crop={'mode': 'fixed', 'region': [0, 0, .7, 1]})]),
                  dict(slot='bottom', clips=[clip('b', 0, 3, 0, crop={'mode': 'fixed', 'region': [.7, 0, .3, .5]})])]
        result = self.render(timeline(None, 3, layout=split, tracks=tracks), 'split')
        self.assertEqual(probe(result.output)['width'], W)

        pip = dict(kind='pip', slots=[full_slot(), dict(name='pip', rect=dict(x=100, y=200, w=60, h=100), z=1)])
        tracks = [dict(slot='main', clips=[clip('m', 0, 4, 0)]),
                  dict(slot='pip', clips=[clip('p', 0, 2, 1, media=dict(kind='color', duration=10, color='white'))])]
        result = self.render(timeline(None, 4, layout=pip, tracks=tracks), 'pip')
        inside = frame(result.output, 2.0).crop((110, 210, 150, 290))
        self.assertGreater(min(inside.convert('L').getdata()), 200)
        early = frame(result.output, 0.5).crop((110, 210, 150, 290))
        self.assertLess(min(early.convert('L').getdata()), 200)

        letter = dict(kind='letterbox', slots=[full_slot()])
        result = self.render(timeline([clip('l', 0, 3, 0, crop={'mode': 'letterbox'})], 3, layout=letter), 'letterbox')
        img = frame(result.output, 1.0).convert('L')
        self.assertLess(max(img.crop((0, 0, W, 40)).getdata()), 30)       # bars above
        self.assertGreater(max(img.crop((0, 140, W, 180)).getdata()), 100)  # picture in the band

    def test_png_overlay_alpha_fade_and_captions(self):
        overlays = [dict(id='card', kind='fact_card', png='card.png', start=1, end=3, rect=dict(x=40, y=60, w=100, h=60)),
                    dict(id='pop', kind='callout', png='card.png', start=0.5, end=3.5, animation='pop',
                         rect=dict(x=40, y=140, w=100, h=60)),
                    dict(id='cap1', kind='caption', png='cap.png', start=0.2, end=3.8, animation='none',
                         rect=dict(x=10, y=240, w=160, h=40))]
        captions = dict(cues=[dict(start=.2, end=3.8, text='hi')], renderer='png', safe_rect=dict(x=8, y=200, w=164, h=100))
        tl = timeline([clip('a', 0, 4, 0, media=dict(kind='color', duration=10, color='black'))], 4,
                      overlays=overlays, captions=captions)
        result = self.render(tl, 'overlay')
        def red(t):
            r, g, b = frame(result.output, t).getpixel((90, 90))
            return r - (g + b) / 2
        self.assertLess(red(0.5), 20)
        self.assertGreater(red(1.06), 20)
        self.assertLess(red(1.06), red(2.0) - 30)   # mid-fade is partially transparent
        self.assertGreater(red(2.0), 150)
        self.assertLess(red(3.5), 20)
        self.assertTrue(result.report['extended_checks']['captions_in_safe_rect'])
        self.assertTrue(result.report['extended_checks']['overlays_in_canvas'])
        y = frame(result.output, 2.0).getpixel((90, 260))
        self.assertGreater(min(y[0], y[1]), 180)

    def test_dip_still_grades_safe_area_and_slide(self):
        still = dict(kind='asset', path='card.png', duration=1, width=100, height=60, license_status='generated')
        clips = [clip('a', 0, 1.5, 0, grade='punchy', crop={'mode': 'fit_blur', 'safe_area': [0, .1, 1, .8]},
                      zoom=[dict(t=.5, zoom=1.2)]),
                 clip('b', 2, 3.5, 1.5, grade='filmic', transition_in=dict(kind='dip', duration=.4)),
                 clip('c', 0, 1.5, 3, media=still, still=True, transition_in=dict(kind='crossfade', duration=.3))]
        overlays = [dict(id='s', kind='hook_title', png='card.png', start=.5, end=3, animation='slide_up',
                         rect=dict(x=40, y=60, w=100, h=60))]
        result = self.render(timeline(clips, 4.5, overlays=overlays), 'mixed')
        self.assertIn('fade=t=out:st=0.8:d=0.2', result.plan.filter_script)  # on the zoomed sub-clip
        self.assertIn("-loop", result.plan.argv)
        self.assertLessEqual(abs(self.video_duration(result.output) - 4.5), FRAME + 1e-3)
        dark = frame(result.output, 1.5).convert('L').crop((0, 0, W, 50))
        self.assertLess(sum(dark.getdata()) / (W * 50), 40)      # dip through black at the join

    def test_narration_ducks_music(self):
        audio = [dict(id='m', media=media('music.m4a', 8), src_in=0, src_out=5, start=0, role='music',
                      duck_under=['narration']),
                 dict(id='n', media=media('voice.m4a', 4), src_in=0, src_out=2, start=2, role='narration')]
        tl = timeline([clip('a', 0, 5, 0)], 5, audio=audio)
        result = self.render(tl, 'duck')
        self.assertIn('sidechaincompress', result.plan.filter_script)
        music_alone = band_level(result.output, 0.6, 1.0, 220)
        music_ducked = band_level(result.output, 2.7, 0.8, 220)
        self.assertGreater(music_alone - music_ducked, 6, (music_alone, music_ducked))
        self.assertTrue(result.report['extended_checks']['loudness'], result.report['loudness'])
        self.assertTrue(result.report['extended_checks']['true_peak'], result.report['loudness'])

    def test_repair_retry_uses_safe_profile(self):
        calls = []
        real = run
        def flaky(args, **kw):
            calls.append(list(args))
            if len(calls) == 1:
                raise StudioError('media_processing_failed', 'boom')
            return real(args, **kw)
        tl = timeline([clip('a', 0, 3, 0, zoom=[dict(t=0, zoom=1, mode='ease'), dict(t=3, zoom=1.3, mode='ease')])], 3)
        with patch('api.editing.render.run', side_effect=flaky):
            result = render_timeline(tl, self.root / 'repair.mp4', self.root / 'work_repair', x264_preset='ultrafast')
        self.assertEqual(result.attempts, 2)
        self.assertIn('-filter_complex', calls[1])
        self.assertNotIn('zoompan', calls[1][calls[1].index('-filter_complex') + 1])
        self.assertTrue(result.output.is_file())

    def test_qa_flags_loudness_drift_and_provenance(self):
        bad = self.root / 'quiet_drift.mp4'
        run(['ffmpeg', '-nostdin', '-y', '-f', 'lavfi', '-i', 'testsrc2=size=180x320:rate=25:duration=3.5',
             '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000:duration=4', '-af', 'volume=-35dB',
             '-c:v', 'libx264', '-preset', 'ultrafast', '-c:a', 'aac', bad])
        tl = timeline([clip('a', 0, 3.5, 0, media=media(license='unknown'))], 3.5)
        report = qa.validate_output(bad, 3.5, (W, H), True, timeline=tl)
        for key in ('score', 'checks', 'flags', 'unverified', 'basis'):
            self.assertIn(key, report)
        self.assertTrue(all(report['checks'].values()))         # legacy gating unchanged
        self.assertFalse(report['extended_checks']['loudness'])
        self.assertLess(report['loudness']['integrated_lufs'], -30)
        self.assertFalse(report['extended_checks']['av_drift'])
        self.assertGreater(report['av_drift'], .3)
        self.assertEqual(report['rights_review_status'], 'manual_review_required')
        self.assertEqual(report['qa_status'], 'flagged')
        self.assertLess(report['score'], 100)

    def test_legacy_render_command_parity(self):
        src = self.root / 'src.mp4'
        info = probe(src)
        for layout in ('preserve', 'center'):
            new_argv, new_dims = render_command(src, self.root / f'new_{layout}.mp4', 1.5, 4, info, '720', layout)
            old_argv, old_dims = legacy_render_command(src, self.root / f'old_{layout}.mp4', 1.5, 4, info, '720', layout)
            self.assertEqual(new_dims, old_dims)
            for flag in ('-ss', '-r', '-c:v', '-crf', '-pix_fmt', '-af', '-c:a', '-b:a', '-movflags'):
                self.assertEqual(new_argv[new_argv.index(flag) + 1].split(',aresample')[0],
                                 old_argv[old_argv.index(flag) + 1], flag)
            self.assertEqual(new_argv[new_argv.index('-i') + 1], str(src))
            self.assertIn('0:a:0?', new_argv)
            run(new_argv, cwd=self.root)
            run(old_argv, cwd=self.root)
            new_report = validate_output(self.root / f'new_{layout}.mp4', 4, new_dims, True)
            old_report = validate_output(self.root / f'old_{layout}.mp4', 4, old_dims, True)
            self.assertEqual(new_report['checks'], old_report['checks'])
            self.assertTrue(all(new_report['checks'].values()))
            # Legacy output started at pts 0.04 (first decoded frame after -ss sits at 0.02 and was
            # rounded up by -r), i.e. video one frame late against audio. The compiler trims and
            # rebases to 0, so legacy frame t+1/25 must equal new frame t.
            self.assertEqual(qa._stream_durations(self.root / f'new_{layout}.mp4')['video'], 4.0)
            for t in (0.5, 2.0, 3.5):
                self.assertLess(mean_diff(frame(self.root / f'new_{layout}.mp4', t),
                                          frame(self.root / f'old_{layout}.mp4', t + FRAME)), 2.0)
            new_info, old_info = probe(self.root / f'new_{layout}.mp4'), probe(self.root / f'old_{layout}.mp4')
            self.assertLess(abs(new_info['duration'] - old_info['duration']), 0.05)


def legacy_render_command(source, output, start, duration, info, resolution, layout, caption=None):
    """Verbatim copy of the pre-WP4 implementation, kept as the parity reference."""
    width, height = (1080, 1920) if resolution == '1080' else (720, 1280)
    if info['height'] < 1080 and resolution == '1080':
        width, height = 720, 1280
    fps = 60 if info['fps'] >= 50 and resolution == '1080' else min(30, info['fps'])
    if layout == 'center':
        graph = f'[0:v]scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},setsar=1[v]'
    else:
        graph = (f'[0:v]split=2[bg][fg];[bg]scale={width}:{height}:force_original_aspect_ratio=increase,'
                 f'crop={width}:{height},boxblur=20:2,eq=brightness=-0.15[back];'
                 f'[fg]scale={width}:{height}:force_original_aspect_ratio=decrease[front];'
                 f'[back][front]overlay=(W-w)/2:(H-h)/2,setsar=1[v]')
    graph += f';[v]eq=contrast=1.015:saturation=1.03,fade=t=in:d=0.12,fade=t=out:st={max(0,duration-.18):.3f}:d=0.18'
    if caption:
        graph += f',ass={caption.name}'
    graph += '[out]'
    command = ['ffmpeg', '-hide_banner', '-nostdin', '-y', '-threads', '2', '-filter_complex_threads', '1',
        '-ss', f'{start:.3f}', '-i', str(source), '-t', f'{duration:.3f}', '-filter_complex', graph,
        '-map', '[out]', '-map', '0:a:0?', '-r', f'{fps:.6f}', '-c:v', 'libx264',
        '-preset', 'veryfast', '-crf', '21', '-pix_fmt', 'yuv420p', '-threads', '2']
    if info['has_audio']:
        command += ['-af', 'loudnorm=I=-16:TP=-1.5:LRA=11,alimiter=limit=0.95', '-c:a', 'aac', '-b:a', '160k']
    command += ['-movflags', '+faststart', '-max_muxing_queue_size', '1024', str(output)]
    return command, (width, height)
