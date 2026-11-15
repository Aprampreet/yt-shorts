"""Small curated word lists (English, romanized Hindi "Hinglish", Devanagari Hindi).

Pure Python, no models. These are heuristics for explainable scoring, not language understanding.
All entries are stored casefolded + NFC so they can be matched against normalised tokens.
"""
import re
import unicodedata

TOKEN_RE = re.compile(r"[\wऀ-ॿ]+(?:['’][\w]+)?")


def norm(text):
    return unicodedata.normalize('NFC', str(text)).replace('\u2019', "'").casefold()


def tokenize(text):
    """Casefolded NFC tokens; keeps Devanagari combining marks inside words."""
    return TOKEN_RE.findall(norm(text))


def _set(*groups):
    return frozenset(norm(w) for g in groups for w in g.split())


# Wh-words and auxiliaries that open a question (auxiliaries only count sentence-initially).
QUESTION_WORDS_EN = _set('what why how who whom whose when where which')
QUESTION_AUX_EN = _set('did do does is are was were can could will would should have has isn\'t aren\'t don\'t')
QUESTION_WORDS_HINGLISH = _set('kya kaise kaisa kaisi kyun kyu kyon kab kaun kaunsa kitna kitne kitni kahan kidhar')
QUESTION_WORDS_HI = _set('क्या कैसे कैसा कैसी क्यों क्यूँ कब कौन कौनसा कितना कितने कितनी कहाँ कहां')
QUESTION_ANY = QUESTION_WORDS_EN | QUESTION_WORDS_HINGLISH | QUESTION_WORDS_HI

SECOND_PERSON = _set('you your yours youre you\'re you\'ll you\'ve aap aapka aapki aapke aapko tum tumhara tumhari tumhe tumko',
                     'आप आपका आपकी आपके आपको तुम तुम्हारा तुम्हारी तुम्हें तुझे')

SUPERLATIVES = _set('best worst biggest greatest fastest most only first last never always ever every insane crazy ultimate',
                    'sabse sabse-zyada pehla pehli aakhri kabhi hamesha',
                    'सबसे पहला पहली आख़िरी कभी हमेशा')

CONTRAST = _set('but however yet instead except nobody nothing never secret mistake wrong truth actually stop dont don\'t'
                ' lekin magar par parantu bas bilkul galti sach raaz rahasya koi-nahi kabhi-nahi',
                'लेकिन मगर परंतु पर गलती ग़लती सच राज़ रहस्य कोई नहीं कभी बस')
CONTRAST_PHRASES = tuple(norm(p) for p in (
    "here's why", 'heres why', 'the truth', 'the secret', 'nobody tells', 'no one tells', 'you won\'t believe',
    'kisi ko nahi pata', 'koi nahi batata', 'yeh sach hai', 'सच यह है', 'कोई नहीं बताता'))

FILLERS = _set('um uh uhh umm er like so basically well okay ok alright actually-so right yeah hmm',
               'toh matlab matlab-ki haan achha accha acha arre',
               'तो मतलब हाँ हां अच्छा अरे ठीक')
FILLER_PHRASES = tuple(norm(p) for p in ('you know', 'i mean', 'kind of', 'sort of', 'hello guys', 'hi guys', 'welcome back',
                                         'दोस्तों'))

EMPHASIS = _set('never always must exactly literally absolutely insane crazy huge massive incredible seriously',
                'bahut zabardast kamaal ekdum bilkul sach mein',
                'बहुत ज़बरदस्त जबरदस्त कमाल एकदम बिलकुल')

SURPRISE = _set('wow whoa suddenly unbelievable shocking shocked surprising unexpected unexpectedly impossible wait',
                'arre are achanak hairan hairaan kamaal',
                'अचानक हैरान चौंका चौंकाने अरे')

RESOLUTION = _set('therefore thus finally result resulted solved answer turns that\'s lesson remember conclusion',
                  'isliye isiliye aakhir natija nateeja samadhan jawab',
                  'इसलिए आखिर आख़िर नतीजा समाधान जवाब')
RESOLUTION_PHRASES = tuple(norm(p) for p in (
    'turns out', "that's why", 'thats why', 'in the end', 'the key is', 'the lesson', 'so remember', 'bottom line',
    'is how', 'इसीलिए', 'यही वजह', 'yahi wajah', 'isi liye'))

HUMOR = _set('haha hahaha lol lmao funny joke hilarious laughing laughed laugh pun prank',
             'hasi mazaak mazak mazedaar hasna comedy',
             'हंसी हँसी मज़ाक मजाक मज़ेदार हंसना')

# Openers that depend on context the clip may not contain.
UNRESOLVED_OPENERS = _set('he she it they them him her his hers its their this that these those then also',
                          'woh wo yeh ye usne unhone usko unko iska uska isne',
                          'वह वो यह ये उसने उन्होंने उसको उनको इसका उसका इसने फिर')
DANGLING_CONJUNCTIONS = _set('and but so because or then also plus though although which while',
                             'aur lekin magar kyunki kyunke toh phir ya jo',
                             'और लेकिन मगर क्योंकि तो फिर या जो')

STOPWORDS = _set(
    'the and for are but not you all any can had her was one our out has have been from they will with this that '
    'than then them there these those their what when where which while who whom whose why how would could should '
    'about after again also because before being below between both cannot does doing down during each from further '
    'here into just more most other over same some such only very your yours yourself itself himself herself '
    'were your its it\'s i\'m i\'ll i\'ve you\'re we\'re they\'re that\'s there\'s what\'s don\'t doesn\'t didn\'t '
    'isn\'t aren\'t wasn\'t won\'t can\'t going gonna wanna really thing things like get got make made much many '
    'will shall might must upon onto under until above against through whether either neither yet still ever',
    'yeh woh jaise lekin magar kyunki kyunke isliye aapka aapki aapke aapko tumhara tumhari tumhe humara hamara '
    'unka unki unke uska uski uske iska iski iske mera meri mere apna apni apne abhi bahut sirf kuch koi kaun kaise '
    'kyun kyunki phir fir bhi hota hoti hote hona hain tha thi the raha rahi rahe kiya kiye karna karte karta '
    'karti wala wali wale waala waali',
    'लेकिन मगर क्योंकि इसलिए आपका आपकी आपके आपको तुम्हारा तुम्हारी तुम्हें हमारा हमारी उनका उनकी उनके उसका उसकी '
    'उसके इसका इसकी इसके मेरा मेरी मेरे अपना अपनी अपने अभी बहुत सिर्फ़ सिर्फ कुछ कोई कौन कैसे क्यों फिर भी होता '
    'होती होते होना हैं था थी थे रहा रही रहे किया किये करना करते करता करती वाला वाली वाले यह वह')
