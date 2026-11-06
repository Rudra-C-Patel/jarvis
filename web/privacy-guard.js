// Jarvis privacy guard. The launcher serves this as Open WebUI's /static/loader.js.
// 1) Removes the browser's cloud speech-recognition API (in Chrome it streams audio to Google),
//    so dictation can only go through Open WebUI's local Whisper on 127.0.0.1.
// 2) Restricts text-to-speech to on-device voices (voice.localService === true). Chrome's
//    "Google ..." voices are network voices and are hidden and never used.
(() => {
  for (const k of ['SpeechRecognition', 'webkitSpeechRecognition', 'SpeechGrammarList',
                   'webkitSpeechGrammarList', 'SpeechRecognitionEvent', 'webkitSpeechRecognitionEvent']) {
    try { delete window[k]; } catch (e) {}
    if (k in window) {
      try { Object.defineProperty(window, k, { value: undefined, configurable: true }); } catch (e) {}
    }
  }
  const ss = window.speechSynthesis;
  if (ss) {
    const allVoices = ss.getVoices.bind(ss);
    const localVoices = () => allVoices().filter(v => v.localService);
    ss.getVoices = localVoices;
    const speak = ss.speak.bind(ss);
    ss.speak = (u) => {
      if (!u.voice || !u.voice.localService) {
        const lang = (u.lang || navigator.language || 'en').slice(0, 2);
        const v = localVoices().find(v => v.lang.startsWith(lang)) || localVoices()[0];
        if (!v) { console.warn('[jarvis] no on-device voice; TTS skipped'); return; }
        u.voice = v;
      }
      speak(u);
    };
  }
  window.__jarvisPrivacyGuard = true;
})();
