"""Language labels are script/word hints; uncertain Romanized Hindi is reviewable."""
import re

HINDI_WORDS = {'hai','hain','nahi','nahin','kya','kaise','aur','yeh','woh','mein','main','karo','karna','bahut','achha','accha','haan','tha','thi','ho','ka','ki','ke','se','ko','toh','ab','dekho','yaar'}

def detect_language(text):
    devanagari = bool(re.search(r'[\u0900-\u097f]', text))
    latin = re.findall(r'[a-z]+', text.lower())
    hindi_roman = sum(word in HINDI_WORDS for word in latin)
    if devanagari and latin:
        return 'mixed'
    if devanagari:
        return 'hi'
    if hindi_roman >= 2 and len(latin) > hindi_roman:
        return 'mixed'
    if hindi_roman >= 2:
        return 'hi-romanized'
    return 'en' if latin else 'und'


def label_cues(cues):
    return [dict(cue, detected_language=cue.get('detected_language') or detect_language(cue['text']),
                 confidence=cue.get('confidence'), translated_english=cue.get('translated_english',''),
                 translated_hindi=cue.get('translated_hindi','')) for cue in cues]


def caption_text(cue, mode):
    original = cue['text'].strip()
    if mode == 'original':
        return original
    if mode == 'english':
        return cue.get('translated_english') or (original if cue.get('detected_language') == 'en' else '')
    if mode == 'hindi':
        return cue.get('translated_hindi') or (original if cue.get('detected_language') == 'hi' else '')
    english = cue.get('translated_english') or (original if cue.get('detected_language') == 'en' else '')
    hindi = cue.get('translated_hindi') or (original if cue.get('detected_language') == 'hi' else '')
    return f'{english}\n{hindi}'.strip() if english and hindi else ''
