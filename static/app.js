        // --- 0. НАСТРОЙКИ ОТБРАЖЕНИЯ (поля ввода и 3D-визуализации) ---
        const UI_PREFS_KEY = 'luch_ui_prefs';
        const uiPrefs = { input: true, three3d: true };
        let threeLoadState = 'idle';   // idle | loading | ready

        function loadUiPrefs() {
            try {
                const raw = localStorage.getItem(UI_PREFS_KEY);
                if (raw) { const p = JSON.parse(raw); if (typeof p.input === 'boolean') uiPrefs.input = p.input; if (typeof p.three3d === 'boolean') uiPrefs.three3d = p.three3d; }
            } catch (e) {}
        }
        function saveUiPrefs() { try { localStorage.setItem(UI_PREFS_KEY, JSON.stringify(uiPrefs)); } catch (e) {} }

        // --- СТАТУС-БАР (как в tmux/vim) ---
        function setStatus(name, text, cls) {
            const el = document.getElementById(name); if (!el) return;
            el.textContent = text; if (cls !== undefined) el.className = el.className.replace(/\b(on|off|busy|err|live)\b/g, '').trim() + ' ' + cls;
        }
        function setCoreState(state, text) {
            const d = document.getElementById('sbLiveDot'); if (d) d.className = 'dot ' + state;
            setStatus('sbState', text);
        }
        function startClock() {
            const t = document.getElementById('sbTime'); if (!t) return;
            const tick = () => { const d = new Date();
                const s = [d.getHours(), d.getMinutes(), d.getSeconds()].map(n => String(n).padStart(2, '0')).join(':');
                t.textContent = s; const p = document.getElementById('promptTs'); if (p) p.textContent = s; };
            tick(); setInterval(tick, 1000);
        }

        // --- ПСЕВДОГРАФИЧЕСКАЯ РАМКА ВОКРУГ ЭКРАНА ---
        function renderFrame() {
            const frame = document.querySelector('.term-frame');
            if (!frame || window.innerWidth <= 760) return;   // на телефоне рамка не рисуется
            // ширину одного знака измеряем, а не угадываем: у моноширинного шрифта она != font-size
            let chW = 8, rowH = 16;
            const probe = document.createElement('span');
            probe.style.cssText = 'position:absolute;visibility:hidden;white-space:pre;font:inherit';
            frame.appendChild(probe);
            probe.textContent = '─'.repeat(100);
            if (probe.getBoundingClientRect().width) chW = probe.getBoundingClientRect().width / 100;
            probe.textContent = '│\n'.repeat(50);
            if (probe.getBoundingClientRect().height) rowH = probe.getBoundingClientRect().height / 50;
            frame.removeChild(probe);
            const cols = Math.max(4, Math.floor(window.innerWidth / chW) - 2);
            const rows = Math.max(2, Math.floor(window.innerHeight / rowH) - 2);
            const set = (id, ch, n) => { const el = document.getElementById(id); if (el) el.textContent = ch.repeat(n); };
            set('frTop', '─', cols); set('frBot', '─', cols);
            set('frLeft', '│\n', rows); set('frRight', '│\n', rows);
        }

        // --- БАННЕР И ЛОГ ЗАПУСКА, печатаются посимвольно ---
        // Буквы заданы по отдельности и выравниваются по кодам: иначе строки разъезжаются.
        const GLYPHS = {
            L: ['██╗', '██║', '██║', '██║', '███████╗', '╚══════╝'],
            U: ['██╗   ██╗', '██║   ██║', '██║   ██║', '██║   ██╗', '╚██████╔╝', '╚═════╝'],
            C: ['██████╗', '██╔════╝', '██║', '██║', '╚██████╗', ' ╚═════╝'],
            H: ['██╗  ██╗', '██║  ██║', '███████║', '██╔══██║', '██║  ██║', '╚═╝  ╚═╝'],
        };
        function makeBanner(word) {
            const letters = [...word].map(c => GLYPHS[c]).filter(Boolean);
            if (!letters.length) return '';
            const w = Math.max(...letters.map(l => Math.max(...l.map(r => [...r].length))));
            const height = Math.max(...letters.map(l => l.length));
            const out = [];
            for (let i = 0; i < height; i++) out.push(letters.map(l => (l[i] || '').padEnd(w, ' ')).join(' '));
            return out.join('\n');
        }
        const BANNER = makeBanner('LUCH');
        const BOOT_LINES = [
            'Luch Terminal v1.7',
            'модули: voice <span class="ok">[OK]</span>  ai <span class="ok">[OK]</span>  web <span class="ok">[OK]</span>  holo <span class="ok">[OK]</span>',
            'введите команду и нажмите Enter · справка: <span class="cmd">/help</span> · очистить: Ctrl+L',
        ];
        function typeBoot() {
            const b = document.getElementById('banner'), l = document.getElementById('bootLine');
            if (!b || !l) return;
            const reduce = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
            if (reduce) { b.textContent = BANNER; l.innerHTML = BOOT_LINES.join('<br>'); return; }
            let i = 0;
            const step = () => {
                if (i < BANNER.length) { b.textContent = BANNER.slice(0, ++i); setTimeout(step, 6); return; }
                b.textContent = BANNER;
                let k = 0;
                const line = () => {
                    if (k < BOOT_LINES.length) { l.innerHTML = BOOT_LINES.slice(0, ++k).join('<br>'); setTimeout(line, 180); }
                };
                line();
            };
            step();
        }

        // --- ИСТОРИЯ КОМАНД (↑ / ↓) И ДОПОЛНЕНИЕ (Tab) ---
        let CMD_HINTS = ['/help', '/clear', '/commands', '/navigator', '/stop_navigator', '/get_my_location',
            '/build_route', '/navigator_status', '/distance_to', '/nearby', '/geocode', '/reverse_geocode',
            '/set_timer', '/lock_pc', '/print_text', '/web_search', '/analyze_movement', '/terminal'];
        const BASE_CMD_HINTS = CMD_HINTS.slice();
        function addCmdHints(list) {
            if (!Array.isArray(list)) return;
            const all = new Set(CMD_HINTS);
            list.forEach(c => all.add(c));
            CMD_HINTS = [...all];
        }
        const cmdHistory = []; let histIdx = -1, histDraft = '';
        function pushHistory(cmd) { if (cmd && cmdHistory[cmdHistory.length - 1] !== cmd) cmdHistory.push(cmd); histIdx = -1; }
        function historyStep(dir) {
            if (!cmdHistory.length) return;
            if (dir < 0) {
                if (histIdx === -1) { histDraft = userInput.value; histIdx = cmdHistory.length; }
                if (histIdx > 0) { histIdx--; userInput.value = cmdHistory[histIdx]; }
                else { histIdx = -1; userInput.value = histDraft; }
            } else {
                if (histIdx === -1) return;
                histIdx++;
                if (histIdx >= cmdHistory.length) { histIdx = -1; userInput.value = histDraft; }
                else userInput.value = cmdHistory[histIdx];
            }
            userInput.focus();
        }
        function completeCmd() {
            const v = userInput.value;
            if (!v.startsWith('/')) return;
            const m = CMD_HINTS.filter(c => c.startsWith(v));
            if (m.length === 1) { userInput.value = m[0] + ' '; userInput.focus(); }
            else if (m.length > 1) { addMessage(m.join('  '), 'system', 'cmd-hint'); }
        }
        function clearScreen() {
            chatContainer.querySelectorAll('.message, .term-boot').forEach(el => el.remove());
            cmdHistory.length = 0; histIdx = -1;
        }

        function toggleInputBar() {
            uiPrefs.input = !uiPrefs.input;
            saveUiPrefs(); applyUiPrefs();
        }

        function toggleThree() {
            uiPrefs.three3d = !uiPrefs.three3d;
            saveUiPrefs();
            if (!uiPrefs.three3d) {
                // сразу гасим оверлей и его анимацию, чтобы не крутился в фоне
                if (typeof hideScreensaver === 'function') hideScreensaver();
                if (sysFrame) { cancelAnimationFrame(sysFrame); sysFrame = null; }
                showCore = false;
                document.getElementById('screensaver-ui').classList.remove('active');
            }
            applyUiPrefs();
        }

        function applyUiPrefs() {
            const bar = document.getElementById('inputContainer');
            if (bar) bar.classList.toggle('hidden', !uiPrefs.input);
            const inBtn = document.getElementById('inputToggle');
            if (inBtn) inBtn.classList.toggle('off', !uiPrefs.input);
            const tBtn = document.getElementById('threeToggle');
            if (tBtn) {
                tBtn.classList.toggle('off', !uiPrefs.three3d);
                tBtn.textContent = uiPrefs.three3d ? '[3D]' : '[3D] OFF';
            }
            setStatus('sbThree', uiPrefs.three3d ? '3D ON' : '3D OFF', uiPrefs.three3d ? 'on' : 'off');
            if (uiPrefs.three3d) ensureThree();
        }

        // ThreeJS грузим только когда 3D реально включён: экономим трафик и CPU
        function ensureThree() {
            if (typeof THREE !== 'undefined') { threeLoadState = 'ready'; return Promise.resolve(true); }
            if (threeLoadState === 'ready') return Promise.resolve(true);
            if (threeLoadState === 'loading') return new Promise(res => { const t = setInterval(() => { if (typeof THREE !== 'undefined') { clearInterval(t); res(true); } }, 100); });
            threeLoadState = 'loading';
            return new Promise(resolve => {
                const done = () => resolve(typeof THREE !== 'undefined');
                const s1 = document.createElement('script');
                s1.src = 'https://cdnjs.cloudflare.com/ajax/libs/three.js/r128/three.min.js';
                s1.onload = () => {
                    const s2 = document.createElement('script');
                    s2.src = 'https://cdn.jsdelivr.net/npm/three@0.128.0/examples/js/controls/OrbitControls.js';
                    s2.onload = done; s2.onerror = done;
                    document.head.appendChild(s2);
                };
                s1.onerror = done;
                document.head.appendChild(s1);
            });
        }

        // --- 1. ОПЦИОНАЛЬНЫЙ ФУНКЦИОНАЛ КОНСОЛИ ---
        const chatContainer = document.getElementById('chatContainer');
        const userInput = document.getElementById('userInput');
        const sendBtn = document.getElementById('sendBtn');
        const micToggle = document.getElementById('micToggle');
        const audioPlayer = document.getElementById('audioPlayer');

        let isStreaming = false, audioContext, analyser, microphone, stream, mediaRecorder = null, audioChunks = [], silenceTimer = null;
        let isSpeaking = false, isProcessingAI = false, isAudioPlaying = false;
        const THRESHOLD = 8, SILENCE_TIMEOUT = 300; let lastUserMsg = null, lastAIMsg = null;

        function addMessage(text, sender, extraClass = '') {
            const msg = document.createElement('div'); msg.className = `message ${sender} ${extraClass}`.trim();
            const d = new Date();
            const ts = document.createElement('span'); ts.className = 'ts';
            ts.textContent = [d.getHours(), d.getMinutes(), d.getSeconds()].map(n => String(n).padStart(2, '0')).join(':');
            const body = document.createElement('span'); body.className = 'body'; body.textContent = text;
            msg.appendChild(ts); msg.appendChild(body);
            // сообщения вставляем ВЫШЕ строки ввода — промпт всегда последний, как в shell
            const prompt = document.getElementById('inputContainer');
            if (prompt && prompt.parentElement === chatContainer) chatContainer.insertBefore(msg, prompt);
            else chatContainer.appendChild(msg);
            chatContainer.scrollTop = chatContainer.scrollHeight; return msg;
        }
        function setText(msg, text) { if (!msg) return; const b = msg.querySelector('.body'); if (b) b.textContent = text; else msg.textContent = text; }
        // снимаем мигающий курсор, когда ответ пришёл
        function settleMsg(msg, text) {
            if (!msg) return;
            msg.classList.remove('pending'); setText(msg, text);
            setCoreState('live', 'READY'); chatContainer.scrollTop = chatContainer.scrollHeight;
        }

        // блок выполненной команды ИИ прямо в чате: команда + аргументы + вывод
        function addAiCommandMsg(cmd) {
            const msg = document.createElement('div');
            msg.className = 'message system ai-cmd-msg ' + (cmd.status === 'error' ? 'error' : '');
            const d = new Date();
            const ts = document.createElement('span'); ts.className = 'ts';
            ts.textContent = [d.getHours(), d.getMinutes(), d.getSeconds()].map(n => String(n).padStart(2, '0')).join(':');

            const head = document.createElement('div'); head.className = 'cmd-head';
            const args = (cmd.args && Object.keys(cmd.args).length) ? ' ' + JSON.stringify(cmd.args) : '';
            head.textContent = `🤖 ${cmd.icon || ''} ${cmd.command}${args}`;
            const meta = document.createElement('span'); meta.className = 'cmd-meta';
            meta.textContent = `${cmd.time || ''}${cmd.elapsed !== undefined ? ' · ' + cmd.elapsed + 'с' : ''}`;
            head.appendChild(meta);

            const out = document.createElement('pre'); out.className = 'cmd-out';
            out.textContent = cmd.output || '(вывод пустой — команда могла ничего не вернуть)';

            const body = document.createElement('span'); body.className = 'body';
            body.appendChild(head); body.appendChild(out);
            msg.appendChild(ts); msg.appendChild(body);

            const prompt = document.getElementById('inputContainer');
            if (prompt && prompt.parentElement === chatContainer) chatContainer.insertBefore(msg, prompt);
            else chatContainer.appendChild(msg);
            chatContainer.scrollTop = chatContainer.scrollHeight;
            // та же запись попадает в журнал панели AI CMD
            pushAiLog(cmd);
            return msg;
        }

        function stopAudio() {
            if (!audioPlayer.paused) { audioPlayer.pause(); audioPlayer.currentTime = 0; isAudioPlaying = false; document.querySelectorAll('.play-audio-btn').forEach(btn => { btn.innerHTML = 'ПРОСЛУШАТЬ'; }); }
        }

        // ---- Приложение ЛУЧ на Android: микрофон открыт постоянно.
        // Фразы выделяются сами по паузам в речи, каждая уходит на сервер,
        // а сервер решает по триггер-слову и по голосу владельца, отвечать или нет.
        const APP_VERSION = '__BUILD__';
        const bridge = window.LuchAndroid;
        const nativeMic = bridge && typeof bridge.startListening === 'function';

        function micOn() {
            isStreaming = true;
            micToggle.classList.add('active');
            setStatus('sbMic', 'СЛУШАЕТ', 'on');
        }
        function micOff() {
            isStreaming = false;
            micToggle.classList.remove('active');
            setStatus('sbMic', 'MIC OFF', '');
            clearTimeout(silenceTimer);
        }

        async function toggleAlwaysListening() {
            if (nativeMic) {
                if (isStreaming) { bridge.stopListening(); micOff(); return; }
                if (!bridge.hasMicPermission()) {
                    alert('Нет разрешения на микрофон — разрешите его в настройках приложения.');
                    return;
                }
                const err = bridge.startListening();
                if (err) { alert('Микрофон: ' + err); return; }
                micOn();
                return;
            }
            // В обычном браузере остаётся путь через страницу
            if (!isStreaming) {
                if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
                    alert('Микрофон в этом браузере недоступен.');
                    return;
                }
                try {
                    if (!audioContext || audioContext.state === 'closed')
                        audioContext = new (window.AudioContext || window.webkitAudioContext)();
                    if (audioContext.state === 'suspended') await audioContext.resume();
                } catch (e) { console.warn('AudioContext', e); }

                try {
                    stream = await getMicStream();
                    microphone = audioContext.createMediaStreamSource(stream); analyser = audioContext.createAnalyser(); analyser.fftSize = 512;
                    microphone.connect(analyser); isStreaming = true;
                    micToggle.classList.add('active'); setStatus('sbMic', 'СЛУШАЕТ', 'on'); startVADListener();
                } catch (err) { alert('Ошибка микрофона: ' + micErrorText(err)); }
            } else {
                micOff();
                if (mediaRecorder && mediaRecorder.state !== 'inactive') mediaRecorder.stop();
                if (stream) stream.getTracks().forEach(t => t.stop()); if (audioContext) audioContext.close();
            }
        }

        // Фразы шлёт сам телефон. Здесь только приём ответа от ИИ.
        window.__luchVoiceResult = function (raw) {
            let data; try { data = JSON.parse(raw); } catch (e) { return; }
            if (!lastUserMsg || lastUserMsg.className.indexOf('user') === -1) lastUserMsg = addMessage('...', 'user');
            if (!lastAIMsg || lastAIMsg.className.indexOf('ai') === -1) { lastAIMsg = addMessage('запрос принят, анализ...', 'ai'); lastAIMsg.classList.add('pending'); }
            setCoreState('idle', 'IDLE');
            if (data.status === 'stop_audio') { stopAudio(); lastUserMsg?.remove(); lastAIMsg?.remove(); lastUserMsg = null; lastAIMsg = null; return; }
            if (data.status === 'ignored') {
                // Молчать здесь нельзя: человек думает, что его не слышат.
                // Пустота без причины — самая обидная поломка.
                if (data.user_text) setText(lastUserMsg, data.user_text);
                if (data.response) settleMsg(lastAIMsg, data.response);
                else { lastUserMsg?.remove(); lastAIMsg?.remove(); }
                lastUserMsg = null; lastAIMsg = null; return;
            }
            setText(lastUserMsg, data.user_text); settleMsg(lastAIMsg, data.response);
            if (data.play_audio) appendPlayButton(lastAIMsg);
            lastUserMsg = null; lastAIMsg = null;
        };

        window.__luchReportVersion = function () {
            const a = window.LuchAndroid; if (a && a.getAppVersion) a.reportPageVersion(APP_VERSION);
            if (location.search.indexOf('selftest') === -1) return;
            // проверка связи телефона с сервером при установке
            (async () => {
                const a2 = window.LuchAndroid;
                const report = { app: a2 && a2.getAppVersion ? a2.getAppVersion() : 'нет моста',
                                page: APP_VERSION, hasHandler: typeof window.__luchVoiceResult === 'function' };
                try {
                    const r = await navigator.mediaDevices; report.mic = r ? 'есть' : 'нет';
                } catch (e) { report.mic = 'нет'; }
                if (a2 && a2.hasMicPermission && a2.hasMicPermission()) {
                    report.start = a2.startListening() || 'микрофон открыт';
                    if (a2.isListening) report.listening = a2.isListening();
                    // короткая фоновая «реплика» для проверки всей цепочки
                    const wav = window.__luchTestWav();
                    report.sent = a2.sendAudioBase64(wav) || 'фраза отправлена';
                } else { report.start = 'нет разрешения на микрофон'; }
                try { await fetch('/_selftest', { method: 'POST', body: JSON.stringify(report) }); } catch (e) { }
            })();
        };

        // тихий WAV 16 кГц моно — только для проверки цепочки при установке
        window.__luchTestWav = function () {
            const rate = 16000, n = rate, buf = new ArrayBuffer(44 + n * 2), v = new DataView(buf);
            const w = (o, s) => { for (let i = 0; i < s.length; i++) v.setUint8(o + i, s.charCodeAt(i)); };
            w(0, 'RIFF'); v.setUint32(4, 36 + n * 2, true); w(8, 'WAVE'); w(12, 'fmt ');
            v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true);
            v.setUint32(24, rate, true); v.setUint32(28, rate * 2, true); v.setUint16(32, 2, true);
            v.setUint16(34, 16, true); w(36, 'data'); v.setUint32(40, n * 2, true);
            for (let i = 0; i < n; i++) v.setInt16(44 + i * 2, Math.round(6000 * Math.sin(2 * Math.PI * 400 * i / rate)), true);
            let s = ''; const u = new Uint8Array(buf);
            for (let i = 0; i < u.length; i += 8192) s += String.fromCharCode.apply(null, u.subarray(i, i + 8192));
            return btoa(s);
        };

        function b64ToBlob(b64, mime) {
            const bin = atob(b64); const arr = new Uint8Array(bin.length);
            for (let i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);
            return new Blob([arr], { type: mime });
        }

        /** Просим микрофон. Если простой запрос не проходит (шумодав, эхоподавление
         *  или занятый другим приложением вход) — повторяем с минимальными
         *  требованиями, это заметно повышает шанс на Android. */
        async function getMicStream() {
            try {
                return await navigator.mediaDevices.getUserMedia({ audio: true, video: false });
            } catch (err) {
                if (err && (err.name === 'NotAllowedError' || err.name === 'SecurityError')) throw err;
                return await navigator.mediaDevices.getUserMedia({
                    audio: { echoCancellation: false, noiseSuppression: false, autoGainControl: false },
                    video: false
                });
            }
        }

        function micErrorText(err) {
            const n = err && err.name, m = (err && err.message) || 'неизвестная ошибка';
            if (n === 'NotAllowedError') return 'нет разрешения на микрофон — разрешите его в настройках приложения.';
            if (n === 'NotFoundError') return 'микрофон не найден.';
            if (n === 'NotReadableError' || /start audio source/i.test(m))
                return 'микрофон занят другим приложением. Закройте другие приложения, использующие камеру или микрофон, и нажмите ещё раз.\n(' + m + ')';
            return m;
        }

        function startVADListener() {
            if (!isStreaming) return; const dataArray = new Uint8Array(analyser.frequencyBinCount);
            function analyze() {
                if (!isStreaming) return; analyser.getByteFrequencyData(dataArray); let sumSquares = 0;
                for (let i = 0; i < dataArray.length; i++) sumSquares += dataArray[i] * dataArray[i];
                const rms = Math.sqrt(sumSquares / dataArray.length);
                if (rms > THRESHOLD && !isProcessingAI) {
                    if (!isSpeaking) { isSpeaking = true; startChunkRecording(); }
                    clearTimeout(silenceTimer); silenceTimer = setTimeout(() => { if (isSpeaking) { isSpeaking = false; stopAndSendChunk(); } }, SILENCE_TIMEOUT);
                }
                requestAnimationFrame(analyze);
            } analyze();
        }

        function startChunkRecording() {
            try {
                let options = {};
                if (MediaRecorder.isTypeSupported('audio/webm')) options = { mimeType: 'audio/webm' }; else if (MediaRecorder.isTypeSupported('audio/mp4')) options = { mimeType: 'audio/mp4' };
                mediaRecorder = new MediaRecorder(stream, options); audioChunks = [];
                mediaRecorder.ondataavailable = e => { if (e.data.size > 0) audioChunks.push(e.data); }; mediaRecorder.start();
            } catch (e) {}
        }

        function stopAndSendChunk() {
            if (mediaRecorder && mediaRecorder.state !== 'inactive') {
                mediaRecorder.onstop = async () => { if (audioChunks.length > 0) await transmitAudioToServer(new Blob(audioChunks, { type: mediaRecorder.mimeType || 'audio/webm' })); };
                mediaRecorder.stop();
            }
        }

        async function transmitAudioToServer(blob) {
            if (isProcessingAI) return; isProcessingAI = true; hideScreensaver();
            if (!lastUserMsg || lastUserMsg.className.indexOf('user') === -1) lastUserMsg = addMessage('...', 'user');
            if (!lastAIMsg || lastAIMsg.className.indexOf('ai') === -1) { lastAIMsg = addMessage('запрос принят, анализ...', 'ai'); lastAIMsg.classList.add('pending'); }
            setCoreState('busy', 'THINKING');
            const ext = (blob.type || 'audio/mp4').indexOf('mp4') >= 0 ? 'm4a' : 'webm';
            const formData = new FormData(); formData.append('file', blob, 's.' + ext);

            try {
                const res = await fetch('/api/voice', { method: 'POST', body: formData }); if (!res.ok) throw new Error('NET FAULT');
                const data = await res.json();
                if (data.status === 'stop_audio' || data.status === 'ignored') { if(data.status==='stop_audio') stopAudio(); lastUserMsg?.remove(); lastAIMsg?.remove(); lastUserMsg = null; lastAIMsg = null; return; }
                setText(lastUserMsg, data.user_text); settleMsg(lastAIMsg, data.response); if (data.play_audio) appendPlayButton(lastAIMsg);
            } catch (err) { settleMsg(lastAIMsg, '⚠ ' + err.message); }
            finally { isProcessingAI = false; lastUserMsg = null; lastAIMsg = null; }
        }

        async function appendPlayButton(msgContainer) {
            const holder = msgContainer.querySelector('.body') || msgContainer;
            const btn = document.createElement('button'); btn.className = 'play-audio-btn'; btn.innerHTML = 'DL...'; btn.disabled = true;
            msgContainer.appendChild(document.createElement('br')); holder.appendChild(btn); msgContainer.scrollIntoView({ block: 'nearest' });
            try {
                const res = await fetch('/api/audio?t=' + Date.now()); if (!res.ok) throw new Error();
                audioPlayer.src = URL.createObjectURL(await res.blob());
                btn.innerHTML = '🔊 ОТВЕТ'; btn.disabled = false;
                btn.onclick = () => {
                    if (!audioPlayer.paused) { audioPlayer.pause(); audioPlayer.currentTime = 0; isAudioPlaying = false; btn.innerHTML = '🔊 ОТВЕТ'; return; }
                    audioPlayer.currentTime = 0; audioPlayer.play().catch(e=>0); isAudioPlaying = true; btn.innerHTML = '⏸ СТОП';
                };
                audioPlayer.play().then(() => { isAudioPlaying = true; btn.innerHTML = '⏸ СТОП'; }).catch(() => 0);
                audioPlayer.onended = () => { isAudioPlaying = false; document.querySelectorAll('.play-audio-btn').forEach(b => b.innerHTML = '🔊 ОТВЕТ'); };
            } catch (err) { btn.innerHTML = 'ERROR'; }
        }

        async function sendMessage() {
            const text = userInput.value.trim(); if (!text) return; hideScreensaver();
            pushHistory(text); userInput.value = ''; userInput.focus();
            if (text === '/clear' || text === '/cls') { clearScreen(); return; }
            if (text === '/commands') {
                const real = menuData ? menuData.groups.flatMap(g => g.items.map(i => '/' + i.name)) : null;
                addMessage((real && real.length ? real : CMD_HINTS).join('   '), 'system', 'cmd-hint');
                addMessage('меню и настройки — кнопки [MENU] и [CFG] в заголовке, либо Ctrl+K и F2', 'system', 'cmd-hint');
                return;
            }
            if (text.startsWith('/')) {
                const sysMsg = addMessage(text, 'user'); setCoreState('busy', 'EXEC');
                try {
                    const res = await fetch('/api/command', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ cmd: text }) });
                    const out = (await res.json()).output;
                    addMessage(out, 'system');
                } catch (err) { addMessage('ERR: ' + err.message, 'system', 'error'); }
                finally { setCoreState('live', 'READY'); } return;
            }
            addMessage(text, 'user'); isProcessingAI = true;
            setCoreState('busy', 'THINKING');
            const aiMsg = addMessage('обработка запроса...', 'ai'); aiMsg.classList.add('pending');
            try {
                const res = await fetch('/api/ask', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ text }) });
                const data = await res.json();
                (data.commands || []).forEach(addAiCommandMsg);
                settleMsg(aiMsg, data.response); if (data.play_audio) appendPlayButton(aiMsg);
            } catch (err) { settleMsg(aiMsg, 'ERR: ' + err.message); aiMsg.classList.add('error'); } finally { isProcessingAI = false; }
        }
        function handleKey(e) {
            const k = e.key;
            if (k === 'Enter') { e.preventDefault(); sendMessage(); return; }
            if (k === 'ArrowUp') { e.preventDefault(); historyStep(-1); return; }
            if (k === 'ArrowDown') { e.preventDefault(); historyStep(1); return; }
            if (k === 'Tab') { e.preventDefault(); completeCmd(); return; }
            if (k === 'l' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); clearScreen(); return; }
            if (k === 'c' && e.ctrlKey && window.getSelection && String(window.getSelection()).length === 0) { e.preventDefault(); userInput.value = ''; userInput.focus(); return; }
        }


        // ══════════════ ПАНЕЛИ CONFIG / MENU ══════════════
        const TOKEN_KEY = 'luch_web_token';
        let openPanel = null, menuData = null, cfgDirty = false, pendingConfirm = null;

        // какой пункт меню открывает какое поле в CONFIG
        const CFG_TARGET = {
            ai: 'provList', provider: 'provList', ai_model: 'provList',
            zen_model: 'provList', zen_api: 'provList', ai_provider: 'provList',
            tts_voice: 'cfgTts', tts_device: 'cfgTtsDevice',
            change_speed_ai_voice: 'cfgSpeed', trigger_word: 'cfgTrigger',
            change_stt_mode: 'cfgStt', whisper_model: 'cfgWhisper', micro: 'cfgMicro'
        };
        const MODE_TAG = { interactive: ['cfg', 'в CONFIG'], danger: ['danger', 'подтвердить'], restart: ['wait', 'перезапуск'] };

        const $ = id => document.getElementById(id);
        function saveToken() { try { localStorage.setItem(TOKEN_KEY, $('cfgToken').value); } catch (e) {} }
        function getToken() { try { return localStorage.getItem(TOKEN_KEY) || ''; } catch (e) { return ''; } }

        async function apiGet(path) {
            const headers = {};
            const token = getToken();
            if (token) headers['X-Luch-Token'] = token;
            const res = await fetch(path, { headers });
            let data = null;
            try { data = await res.json(); } catch (e) { data = null; }
            if (res.status === 401) {
                const err = new Error('Нужен web_token — введи его в поле «web_token» в CONFIG');
                err.needsToken = true;
                throw err;
            }
            if (!res.ok) throw new Error((data && data.detail) || ('HTTP ' + res.status));
            return data;
        }

        async function apiPost(path, body) {
            const headers = { 'Content-Type': 'application/json' };
            const token = getToken();
            if (token) headers['X-Luch-Token'] = token;
            const res = await fetch(path, { method: 'POST', headers, body: JSON.stringify(body || {}) });
            let data = null;
            try { data = await res.json(); } catch (e) { data = null; }
            if (res.status === 401) {
                const err = new Error('Нужен web_token — введи его в поле «web_token» в CONFIG');
                err.needsToken = true;
                throw err;
            }
            if (!res.ok) throw new Error((data && (data.detail || data.output)) || ('HTTP ' + res.status));
            return data;
        }

        async function apiDelete(path) {
            const headers = {};
            const token = getToken();
            if (token) headers['X-Luch-Token'] = token;
            const res = await fetch(path, { method: 'DELETE', headers });
            let data = null;
            try { data = await res.json(); } catch (e) { data = null; }
            if (res.status === 401) throw new Error('Нужен web_token');
            if (!res.ok) throw new Error((data && data.detail) || ('HTTP ' + res.status));
            return data;
        }

        function say(where, text, cls) {
            const box = $(where);
            if (!box) return;
            const line = document.createElement('div');
            if (cls) line.className = cls;
            line.textContent = text;
            box.appendChild(line);
            box.scrollTop = box.scrollHeight;
        }
        function clearLog(where) { const b = $(where); if (b) b.textContent = ''; }
        function busy(btn, on) { if (btn) { btn.disabled = !!on; btn.dataset.label = btn.dataset.label || btn.textContent; btn.textContent = on ? '…' : btn.dataset.label; } }

        function togglePanel(name) {
            const panel = $(name === 'cfg' ? 'cfgPanel' : (name === 'ai' ? 'aiPanel' : 'menuPanel'));
            if (!panel) return;
            if (openPanel === name) { panel.hidden = true; openPanel = null; $('cfgToggle')?.classList.remove('on'); $('menuToggle')?.classList.remove('on'); $('aiToggle')?.classList.remove('on'); if (uiPrefs.input) userInput.focus(); return; }
            document.querySelectorAll('.panel').forEach(p => { if (p !== panel) p.hidden = true; });
            $('cfgToggle')?.classList.toggle('on', name === 'cfg');
            $('menuToggle')?.classList.toggle('on', name === 'menu');
            $('aiToggle')?.classList.toggle('on', name === 'ai');
            openPanel = name;
            panel.hidden = false;
            hideScreensaver();
            if (name === 'cfg') loadConfig();
            else if (name === 'ai') loadAiCommands(true);
            else loadMenu();
        }

        // ─────────── CONFIG: единый центр провайдеров ───────────
        let provList = [];      // [{kind:'ollama'|'zen'|'custom', id, name, base_url, model, has_key}]
        let provSel = null;     // выбранный провайдер (тот же формат)

        const CFG_FIELDS = ['cfgName', 'cfgBaseUrl', 'cfgApiKey', 'cfgModel', 'cfgTimeout'];
        function markDirty() {
            cfgDirty = true;
            CFG_FIELDS.forEach(id => $(id)?.classList.add('dirty'));
        }
        function clearDirty() {
            cfgDirty = false;
            CFG_FIELDS.forEach(id => $(id)?.classList.remove('dirty'));
        }

        function curProvider() { return provSel; }

        function renderProviders() {
            const box = $('provList');
            if (!box) return;
            box.textContent = '';
            if (!provList.length) {
                box.innerHTML = '<div class="log dim">провайдеры недоступны</div>';
                return;
            }
            provList.forEach(p => {
                const b = document.createElement('button');
                b.type = 'button';
                b.className = 'prov-card' + (provSel && provSel.uid === p.uid ? ' on' : '');
                const top = document.createElement('div');
                top.className = 'prov-top';
                const nm = document.createElement('span');
                nm.className = 'prov-name';
                nm.textContent = p.name;
                top.appendChild(nm);
                if (p.active) {
                    const tag = document.createElement('span');
                    tag.className = 'prov-tag on';
                    tag.textContent = 'активен';
                    top.appendChild(tag);
                }
                b.appendChild(top);
                const sub = document.createElement('div');
                sub.className = 'prov-sub';
                sub.textContent = (p.detail || '') + (p.has_key ? '  ·  ключ задан' : '');
                b.appendChild(sub);
                b.onclick = () => selectProvider(p);
                box.appendChild(b);
            });
        }

        async function selectProvider(p, activate = true) {
            provSel = p;
            renderProviders();
            const custom = p.kind === 'custom';
            $('cfgCardTitle').textContent = custom
                ? 'КАСТОМНЫЙ ПРОВАЙДЕР — ' + p.name.toUpperCase()
                : (p.kind === 'zen' ? 'OPENCODE ZEN' : 'OLLAMA');
            $('cfgName').value = custom ? p.name : '';
            $('cfgBaseUrl').value = custom ? (p.base_url || '') : (p.base_url || '');
            $('cfgModel').value = p.model || '';
            $('cfgTimeout').value = p.timeout || 120;
            $('cfgApiKey').value = '';
            $('cfgApiKey').placeholder = p.has_key
                ? `ключ сохранён (${p.key_length} симв.) — пустое поле его не изменит`
                : 'необязательно — оставь пустым';
            $('cfgSub').textContent = p.name + ' · ' + (p.model || '—');
            onProviderChange();
            if (p.kind === 'ollama' && p.models) fillModelList(p.models, p.model);
            if (activate && !custom) {
                // встроенные провайдеры переключаются сразу
                try {
                    const d = await apiPost('/api/ai/switch', { provider: p.kind });
                    if (d.config) applyConfig(d.config, { keepCard: true });
                    clearLog('cfgAiLog');
                    say('cfgAiLog', 'провайдер: ' + (d.config ? d.config.summary : p.name), 'ok');
                    refreshStateBadge();
                } catch (e) { say('cfgAiLog', e.message, 'err'); }
            }
            if (activate && custom) {
                try {
                    const d = await apiPost('/api/ai/switch', { provider: p.id });
                    if (d.config) applyConfig(d.config, { keepCard: true });
                    say('cfgAiLog', 'провайдер: ' + p.name, 'ok');
                    refreshStateBadge();
                } catch (e) { say('cfgAiLog', e.message, 'err'); }
            }
        }

        function fillModelList(models, current) {
            const dl = $('cfgModelList');
            if (!dl) return;
            dl.innerHTML = '';
            (current ? [current] : []).concat(models || []).forEach(m => {
                const o = document.createElement('option'); o.value = m; dl.appendChild(o);
            });
        }

        function onProviderChange() {
            const p = curProvider();
            const kind = p ? p.kind : 'custom';
            const custom = kind === 'custom';
            $('cfgName').disabled = !custom;
            $('cfgBaseUrl').disabled = !custom;
            $('cfgApiKey').disabled = false;
            $('cfgModel').disabled = false;
            $('cfgTimeout').disabled = !custom;
            const notes = {
                custom: 'Любой OpenAI-совместимый адрес: vLLM, LM Studio, llama.cpp, OpenRouter, DeepSeek, прокси. Ключ можно не указывать.',
                ollama: 'Локальные модели Ollama. Список берётся из самого Ollama, ключ не нужен.',
                zen: 'Облачные модели OpenCode Zen. Введи API-ключ и модель — этого достаточно.'
            };
            $('cfgAiNote').textContent = notes[kind] || '';
        }
        function onBaseUrlInput() {
            if ($('cfgApiKey')) $('cfgApiKey').placeholder = 'ключ для этого адреса';
            markDirty();
        }

        function applyConfig(cfg, opts) {
            provList = buildProviderList(cfg);
            const active = provList.find(p => p.active) || provList[0] || null;
            if (opts && opts.keepCard && provSel) {
                const same = provList.find(p => p.uid === provSel.uid);
                if (same) same.active = same.active || active.active;
                renderProviders();
                $('cfgSub').textContent = cfg.summary || $('cfgSub').textContent;
                return;
            }
            renderProviders();
            if (active) {
                const keepName = $('cfgName') ? $('cfgName').value : '';
                selectProvider(active, false);
                if (active.kind === 'custom' && !keepName) $('cfgName').value = active.name;
            }
            $('cfgSub').textContent = cfg.summary || '';
            if (cfg.provider === 'zen') fillModelList([], cfg.zen_model);
            clearDirty();
        }

        function buildProviderList(cfg) {
            const out = [];
            const actCustom = cfg.active_custom_id;
            const isCustom = ['openai', 'custom', 'api', 'compatible'].includes(cfg.provider);
            (cfg.custom_providers || []).forEach(p => {
                out.push({
                    uid: 'custom:' + p.id, kind: 'custom', id: p.id, name: p.name || p.id,
                    base_url: p.base_url, model: p.model, timeout: p.timeout,
                    has_key: p.has_key, key_length: p.key_length,
                    detail: p.base_url + ' · ' + (p.model || 'без модели'),
                    active: isCustom && p.id === actCustom
                });
            });
            if (!out.some(p => p.active) && isCustom && cfg.base_url) {
                out.push({
                    uid: 'custom:' + (actCustom || 'current'), kind: 'custom', id: actCustom || 'current',
                    name: cfg.provider_label || cfg.base_url, base_url: cfg.base_url,
                    model: cfg.model, timeout: cfg.timeout, has_key: cfg.has_key,
                    key_length: cfg.key_length, detail: cfg.base_url + ' · ' + (cfg.model || 'без модели'),
                    active: true
                });
            }
            out.push({
                uid: 'ollama', kind: 'ollama', id: 'ollama', name: 'Ollama',
                base_url: cfg.ollama_url, model: cfg.ollama_model, models: cfg.ollama_models || [],
                has_key: false, detail: 'локально · ' + (cfg.ollama_models || []).length + ' моделей',
                active: cfg.provider === 'ollama'
            });
            out.push({
                uid: 'zen', kind: 'zen', id: 'zen', name: 'OpenCode Zen',
                base_url: 'https://opencode.ai/zen/v1', model: cfg.zen_model,
                has_key: cfg.zen_has_key, key_length: cfg.zen_key_length,
                detail: 'облако · gpt-5.x, o3 и другие', active: cfg.provider === 'zen'
            });
            return out;
        }

        async function loadConfig() {
            $('cfgToken').value = getToken();
            try {
                const [cfg, st] = await Promise.all([
                    fetch('/api/ai/config').then(r => r.json()),
                    fetch('/api/settings').then(r => r.json())
                ]);
                if (cfg && !cfg.detail) applyConfig(cfg);
                if (st && st.settings) {
                    const s = st.settings;
                    $('cfgTts').value = s.tts_voice ?? '';
                    $('cfgTtsDevice').value = s.tts_device || 'auto';
                    $('cfgSpeed').value = s.speed_ai_speak ?? '';
                    $('cfgTrigger').value = s.trigger_word ?? '';
                    $('cfgStt').value = s.stt_mode || 'whisper';
                    $('cfgMicro').value = s.micro_index ?? '';
                    $('cfgWhisper').value = s.whispermodel ?? '';
                }
                clearDirty();
            } catch (e) { say('cfgAiLog', 'не удалось загрузить настройки: ' + e.message, 'err'); }
        }

        function collectAi() {
            const p = curProvider();
            const kind = p ? p.kind : 'custom';
            return {
                name: $('cfgName') ? $('cfgName').value.trim() : '',
                base_url: $('cfgBaseUrl').value.trim(),
                api_key: $('cfgApiKey').value,
                model: $('cfgModel').value.trim(),
                timeout: $('cfgTimeout').value ? Number($('cfgTimeout').value) : null,
                provider_id: (kind === 'custom' && p) ? p.id : '',
                keep_key: true,
                activate: true
            };
        }
        function markBad(field, bad) { $(field)?.classList.toggle('bad', bad); }

        async function newCustomProvider() {
            const fresh = {
                uid: 'custom:__new__', kind: 'custom', id: '', name: 'Новый провайдер',
                base_url: '', model: '', timeout: 120, has_key: false, key_length: 0,
                detail: 'введи адрес, ключ и модель', active: false
            };
            provSel = fresh;
            renderProviders();
            $('cfgCardTitle').textContent = 'НОВЫЙ КАСТОМНЫЙ ПРОВАЙДЕР';
            $('cfgName').value = '';
            $('cfgBaseUrl').value = '';
            $('cfgApiKey').value = '';
            $('cfgApiKey').placeholder = 'необязательно — оставь пустым';
            $('cfgModel').value = '';
            $('cfgTimeout').value = 120;
            onProviderChange();
            $('cfgName').focus();
        }

        async function deleteSelectedProvider() {
            const p = curProvider();
            if (!p || p.kind !== 'custom' || !p.id) {
                say('cfgAiLog', 'выбранный провайдер не удаляется — он встроенный', 'warn');
                return;
            }
            if (!confirm('Удалить провайдера «' + p.name + '»?')) return;
            clearLog('cfgAiLog');
            try {
                const d = await apiDelete('/api/ai/provider?provider_id=' + encodeURIComponent(p.id));
                applyConfig(d.config);
                say('cfgAiLog', 'удалён: ' + d.removed, 'ok');
                refreshStateBadge();
            } catch (e) { say('cfgAiLog', e.message, 'err'); }
        }

        async function fetchAiModels(btn) {
            clearLog('cfgAiLog');
            const url = $('cfgBaseUrl').value.trim();
            if (!url) { say('cfgAiLog', 'сначала укажи base_url', 'err'); markBad('cfgBaseUrl', true); return; }
            markBad('cfgBaseUrl', false);
            busy(btn, true);
            try {
                const q = new URLSearchParams({ base_url: url, api_key: $('cfgApiKey').value });
                const d = await fetch('/api/ai/models?' + q).then(r => r.json());
                if (d.models && d.models.length) {
                    const dl = $('cfgModelList');
                    dl.innerHTML = '';
                    d.models.forEach(m => { const o = document.createElement('option'); o.value = m; dl.appendChild(o); });
                    say('cfgAiLog', `загружено моделей: ${d.models.length}`, 'ok');
                    say('cfgAiLog', d.models.slice(0, 12).join('   '), 'dim');
                    if (!$('cfgModel').value) $('cfgModel').value = d.models[0];
                } else {
                    say('cfgAiLog', d.error || 'модели не вернулись', 'warn');
                    say('cfgAiLog', d.hint || 'впиши имя модели вручную', 'dim');
                }
            } catch (e) { say('cfgAiLog', 'ошибка: ' + e.message, 'err'); }
            finally { busy(btn, false); }
        }

        async function testAiEndpoint(btn) {
            clearLog('cfgAiLog');
            const p = curProvider();
            const c = collectAi();
            if (!c.model) { say('cfgAiLog', 'укажи модель', 'err'); markBad('cfgModel', true); return; }
            busy(btn, true);
            try {
                if (p && p.kind === 'zen') {
                    const d = await apiPost('/api/ai/test', { base_url: p.base_url, api_key: c.api_key, model: c.model });
                    say('cfgAiLog', d.ok ? 'связь работает' : 'связь не работает', d.ok ? 'ok' : 'err');
                    say('cfgAiLog', d.message, d.ok ? 'dim' : 'err');
                    return;
                }
                if (p && p.kind === 'ollama') {
                    const d = await apiPost('/api/ai/test', { base_url: p.base_url, api_key: '', model: c.model });
                    say('cfgAiLog', d.ok ? 'связь работает' : 'связь не работает', d.ok ? 'ok' : 'err');
                    say('cfgAiLog', d.message, d.ok ? 'dim' : 'err');
                    return;
                }
                if (!c.base_url) { say('cfgAiLog', 'укажи base_url', 'err'); markBad('cfgBaseUrl', true); return; }
                markBad('cfgBaseUrl', false);
                say('cfgAiLog', `проверяю ${c.model} на ${c.base_url} …`, 'dim');
                const d = await apiPost('/api/ai/test', { base_url: c.base_url, api_key: c.api_key, model: c.model });
                say('cfgAiLog', d.ok ? 'связь работает' : 'связь не работает', d.ok ? 'ok' : 'err');
                say('cfgAiLog', d.message, d.ok ? 'dim' : 'err');
            } catch (e) { say('cfgAiLog', e.message, 'err'); }
            finally { busy(btn, false); }
        }

        async function saveAiEndpoint(btn) {
            clearLog('cfgAiLog');
            const p = curProvider();
            const c = collectAi();
            const kind = p ? p.kind : 'custom';

            if (kind === 'ollama') {
                if (!c.model) { say('cfgAiLog', 'укажи модель', 'err'); markBad('cfgModel', true); return; }
                busy(btn, true);
                try {
                    await apiPost('/api/ai/switch', { provider: 'ollama' });
                    const res = await apiPost('/api/ai/ollama', { model: c.model });
                    applyConfig(res.config);
                    clearDirty();
                    say('cfgAiLog', 'Ollama · ' + c.model, 'ok');
                    refreshStateBadge();
                } catch (e) { say('cfgAiLog', e.message, 'err'); }
                finally { busy(btn, false); }
                return;
            }

            if (kind === 'zen') {
                if (!c.api_key && !p.has_key) { say('cfgAiLog', 'нужен API-ключ Zen', 'err'); markBad('cfgApiKey', true); return; }
                if (!c.model) { say('cfgAiLog', 'укажи модель', 'err'); markBad('cfgModel', true); return; }
                busy(btn, true);
                try {
                    await apiPost('/api/ai/zen', { api_key: c.api_key, model: c.model });
                    const d = await apiPost('/api/ai/switch', { provider: 'zen' });
                    applyConfig(d.config);
                    clearDirty();
                    say('cfgAiLog', 'OpenCode Zen · ' + c.model, 'ok');
                    refreshStateBadge();
                } catch (e) { say('cfgAiLog', e.message, 'err'); }
                finally { busy(btn, false); }
                return;
            }

            if (!c.base_url) { say('cfgAiLog', 'укажи base_url', 'err'); markBad('cfgBaseUrl', true); return; }
            if (!c.model) { say('cfgAiLog', 'укажи модель', 'err'); markBad('cfgModel', true); return; }
            markBad('cfgBaseUrl', false); markBad('cfgModel', false);
            busy(btn, true);
            try {
                const d = await apiPost('/api/ai/config', c);
                applyConfig(d.config);
                clearDirty();
                $('cfgApiKey').value = '';
                say('cfgAiLog', 'сохранено: ' + d.config.summary, 'ok');
                say('cfgAiLog', 'адрес запроса: ' + d.config.chat_url, 'dim');
                refreshStateBadge();
            } catch (e) { say('cfgAiLog', e.message, 'err'); }
            finally { busy(btn, false); }
        }

        async function saveVoiceSettings(btn) {
            clearLog('cfgVoiceLog');
            const pairs = [
                ['tts_voice', $('cfgTts').value], ['tts_device', $('cfgTtsDevice').value],
                ['speed_ai_speak', $('cfgSpeed').value],
                ['trigger_word', $('cfgTrigger').value], ['stt_mode', $('cfgStt').value],
                ['micro_index', $('cfgMicro').value]
            ].filter(([, v]) => String(v).trim() !== '');
            if (!pairs.length) { say('cfgVoiceLog', 'нечего применять', 'warn'); return; }
            busy(btn, true);
            let bad = 0;
            for (const [key, value] of pairs) {
                try {
                    const d = await apiPost('/api/settings', { key, value: String(value).trim() });
                    say('cfgVoiceLog', `${key} = ${d.value}`, 'ok');
                    if (d.warning) say('cfgVoiceLog', d.warning, 'warn');
                } catch (e) { bad++; say('cfgVoiceLog', `${key}: ${e.message}`, 'err'); }
            }
            if (!bad) say('cfgVoiceLog', 'готово', 'dim');
            refreshStateBadge();
            busy(btn, false);
        }

        // ─────────── MENU ───────────
        async function loadMenu() {
            const body = $('menuBody');
            if (menuData) { renderMenu(); return; }
            body.textContent = 'загрузка меню…';
            try {
                const d = await fetch('/api/menu').then(r => r.json());
                if (d && !d.detail) { menuData = d; renderMenu(); }
                else body.textContent = d.detail || 'меню недоступно';
            } catch (e) { body.textContent = 'ошибка: ' + e.message; }
        }

        function renderMenu() {
            const body = $('menuBody');
            body.textContent = '';
            (menuData.groups || []).forEach(g => {
                const gr = document.createElement('div'); gr.className = 'menu-group';
                const t = document.createElement('div'); t.className = 'menu-group-t'; t.textContent = g.title;
                gr.appendChild(t);
                g.items.forEach(it => {
                    const b = document.createElement('button');
                    b.className = 'menu-item';
                    b.type = 'button';
                    const cmd = document.createElement('span'); cmd.className = 'cmd'; cmd.textContent = it.name;
                    const desc = document.createElement('span'); desc.className = 'desc'; desc.textContent = it.desc || '';
                    b.appendChild(cmd); b.appendChild(desc);
                    const tagInfo = MODE_TAG[it.mode];
                    if (tagInfo) {
                        const tag = document.createElement('span'); tag.className = 'tag ' + tagInfo[0]; tag.textContent = tagInfo[1];
                        b.appendChild(tag);
                    }
                    b.onclick = () => onMenuItem(it);
                    gr.appendChild(b);
                });
                body.appendChild(gr);
            });
            if (menuData.ai_commands && menuData.ai_commands.length) {
                const box = document.createElement('div'); box.className = 'menu-ai';
                const t = document.createElement('div'); t.className = 'menu-ai-t';
                t.textContent = `КОМАНДЫ ИИ (${menuData.ai_commands.length}) — ВЫЗЫВАЕТ САМ ИИ`;
                box.appendChild(t);
                const list = document.createElement('div'); list.className = 'menu-ai-list';
                menuData.ai_commands.forEach(c => {
                    const row = document.createElement('button');
                    row.type = 'button'; row.className = 'ai-cmd';
                    row.title = c.example ? 'пример: ' + c.example : (c.desc || '');
                    const ic = document.createElement('span'); ic.className = 'ai-ic'; ic.textContent = c.icon || '•';
                    const nm = document.createElement('span'); nm.className = 'ai-nm'; nm.textContent = c.name;
                    const ds = document.createElement('span'); ds.className = 'ai-ds'; ds.textContent = c.desc || '';
                    row.appendChild(ic); row.appendChild(nm); row.appendChild(ds);
                    if (c.example) {
                        row.onclick = () => {
                            const inp = $('userInput');
                            if (!inp) return;
                            inp.value = c.example;
                            inp.focus();
                            if (inp.setSelectionRange) inp.setSelectionRange(inp.value.length, inp.value.length);
                            if (openPanel) togglePanel(openPanel);
                        };
                    }
                    list.appendChild(row);
                });
                box.appendChild(list);
                if (menuData.ai_commands_help) {
                    const help = document.createElement('div');
                    help.className = 'menu-ai-hint';
                    help.textContent = 'клик по команде подставляет её пример в строку ввода';
                    box.appendChild(help);
                }
                body.appendChild(box);
            }
        }

        // ── журнал выполненных команд: видно, что запускали и что вернулось ──
        const cmdLog = [];
        function pushCmdLog(name, output, kind) {
            cmdLog.push({ name, output, kind: kind || 'ok', time: new Date().toLocaleTimeString('ru-RU') });
            while (cmdLog.length > 30) cmdLog.shift();
            renderCmdLog();
        }
        function renderCmdLog() {
            const el = $('menuOut');
            if (!el) return;
            el.textContent = '';
            if (!cmdLog.length) {
                const e = document.createElement('div');
                e.className = 'log dim';
                e.textContent = 'здесь появится вывод выполненных команд';
                el.appendChild(e);
                return;
            }
            cmdLog.forEach(r => {
                const head = document.createElement('div');
                head.className = 'out-head ' + (r.kind || '');
                head.textContent = `[${r.time}] /${r.name}`;
                el.appendChild(head);
                const body = document.createElement('pre');
                body.className = 'out-body';
                body.textContent = r.output || 'OK';
                el.appendChild(body);
            });
            el.scrollTop = el.scrollHeight;
        }

        // ─────────── AI CMD: команды ИИ, их аргументы и вывод ───────────
        let aiCmds = [];        // [{name, icon, desc, example, args:[{name,required,default,type}], danger}]
        let aiPending = null;   // имя опасной команды, ждущей второго клика
        const aiRows = {};      // name -> {el, inputs:{}}
        const aiLog = [];       // [{time, command, args, status, output, elapsed}]

        function renderAiCommands() {
            const body = $('aiBody');
            if (!body) return;
            body.textContent = '';
            if (!aiCmds.length) {
                body.appendChild(Object.assign(document.createElement('div'),
                    { className: 'log dim', textContent: 'команды ИИ не найдены' }));
                return;
            }
            const head = document.createElement('div');
            head.className = 'menu-ai-t';
            head.textContent = `КОМАНДЫ ИИ (${aiCmds.length})`;
            body.appendChild(head);

            aiCmds.forEach(c => {
                const row = document.createElement('div'); row.className = 'ai-row';

                const top = document.createElement('button');
                top.type = 'button'; top.className = 'ai-cmd';
                const ic = document.createElement('span'); ic.className = 'ai-ic'; ic.textContent = c.icon || '•';
                const nm = document.createElement('span'); nm.className = 'ai-nm'; nm.textContent = c.name;
                const ds = document.createElement('span'); ds.className = 'ai-ds'; ds.textContent = c.desc || '';
                top.appendChild(ic); top.appendChild(nm); top.appendChild(ds);
                if (c.danger) {
                    const tg = document.createElement('span'); tg.className = 'tag danger'; tg.textContent = 'опасно';
                    top.appendChild(tg);
                }
                const ar = document.createElement('span'); ar.className = 'ai-arrow'; ar.textContent = '▾';
                top.appendChild(ar);

                const form = document.createElement('div'); form.className = 'ai-form';
                const inputs = {};
                let fromExample = {};
                try { fromExample = (JSON.parse(c.example || '{}').args) || {}; } catch (e) { fromExample = {}; }

                (c.args || []).forEach(a => {
                    const lab = document.createElement('label'); lab.className = 'row';
                    const key = document.createElement('span'); key.className = 'row-k';
                    key.textContent = a.name + (a.required ? '*' : '');
                    const inp = document.createElement('input');
                    inp.className = 'inp'; inp.type = 'text'; inp.spellcheck = false;
                    const prefill = fromExample[a.name] !== undefined ? fromExample[a.name]
                        : (a.default !== null && a.default !== undefined ? a.default : '');
                    inp.value = prefill === null ? '' : String(prefill);
                    inp.placeholder = a.required ? 'обязательно' : 'необязательно';
                    lab.appendChild(key); lab.appendChild(inp);
                    form.appendChild(lab);
                    inputs[a.name] = inp;
                });
                if (!(c.args || []).length) {
                    const none = document.createElement('div'); none.className = 'note';
                    none.textContent = 'аргументы не нужны';
                    form.appendChild(none);
                }

                const btns = document.createElement('div'); btns.className = 'btn-row';
                const run = document.createElement('button');
                run.type = 'button'; run.className = 'btn btn-primary';
                run.textContent = '▶ выполнить';
                run.onclick = () => runAiCommand(c, inputs, run);
                btns.appendChild(run);

                const ex = document.createElement('button');
                ex.type = 'button'; ex.className = 'btn';
                ex.textContent = 'вставить в чат';
                ex.title = 'подставить пример команды в строку ввода и отправить ИИ';
                ex.onclick = () => {
                    const args = {};
                    Object.keys(inputs).forEach(k => { if (inputs[k].value.trim() !== '') args[k] = inputs[k].value.trim(); });
                    const payload = 'command ' + JSON.stringify({ command: c.name, args });
                    const inp = $('userInput');
                    if (!inp) return;
                    inp.value = payload; inp.focus();
                    if (openPanel) togglePanel(openPanel);
                };
                btns.appendChild(ex);
                form.appendChild(btns);

                top.onclick = () => {
                    const open = row.classList.toggle('open');
                    ar.textContent = open ? '▴' : '▾';
                };

                row.appendChild(top); row.appendChild(form);
                body.appendChild(row);
                aiRows[c.name] = { el: row, inputs };
            });

            const hint = document.createElement('div');
            hint.className = 'menu-ai-hint';
            hint.textContent = '* — обязательный аргумент. Команды с тегом «опасно» выполняются после подтверждения.';
            body.appendChild(hint);
        }

        async function loadAiCommands(force) {
            const body = $('aiBody');
            if (aiCmds.length && !force) { renderAiCommands(); return; }
            if (body && !aiCmds.length) body.textContent = 'загрузка команд ИИ…';
            try {
                const d = await apiGet('/api/ai/commands');
                const firstLoad = !aiCmds.length;
                aiCmds = d.commands || [];
                if (firstLoad) renderAiCommands();
                if (d.history) {
                    aiLog.length = 0;
                    (d.history || []).slice(-30).forEach(h => aiLog.push(h));
                }
                renderAiLog();
                $('aiSub').textContent = `${aiCmds.length} команд(ы), ${aiLog.length} в журнале`;
            } catch (e) {
                if (body) body.textContent = 'не удалось загрузить команды ИИ: ' + e.message;
            }
        }

        function renderAiLog() {
            const el = $('aiOut');
            if (!el) return;
            el.textContent = '';
            if (!aiLog.length) {
                el.appendChild(Object.assign(document.createElement('div'),
                    { className: 'log dim', textContent: 'здесь появится вывод команд ИИ' }));
                return;
            }
            aiLog.forEach(r => {
                const head = document.createElement('div');
                head.className = 'out-head ' + (r.status === 'error' ? 'err' : (r.status === 'confirm_required' ? 'warn' : ''));
                const args = (r.args && Object.keys(r.args).length)
                    ? ' ' + JSON.stringify(r.args) : '';
                head.textContent = `[${r.time}] ${r.icon || ''} ${r.command}${args} — ${r.elapsed}с`;
                el.appendChild(head);
                const body = document.createElement('pre');
                body.className = 'out-body';
                body.textContent = r.output || 'OK';
                el.appendChild(body);
            });
            el.scrollTop = el.scrollHeight;
        }

        function pushAiLog(entry) {
            aiLog.push(entry);
            while (aiLog.length > 30) aiLog.shift();
            renderAiLog();
            $('aiSub').textContent = `${aiCmds.length} команд(ы), ${aiLog.length} в журнале`;
        }

        async function runAiCommand(c, inputs, btn) {
            clearLog('aiLog');
            const args = {};
            (c.args || []).forEach(a => {
                const v = inputs[a.name] ? inputs[a.name].value.trim() : '';
                if (v !== '') args[a.name] = v;
            });
            const missing = (c.args || []).filter(a => a.required && !(a.name in args)).map(a => a.name);
            if (missing.length) {
                say('aiLog', `заполни обязательные аргументы: ${missing.join(', ')}`, 'err');
                missing.forEach(n => inputs[n] && inputs[n].classList.add('bad'));
                return;
            }
            (c.args || []).forEach(a => inputs[a.name] && inputs[a.name].classList.remove('bad'));

            const send = confirm => {
                busy(btn, true);
                say('aiLog', `${c.icon || ''} ${c.name} ${JSON.stringify(args)}`, 'dim');
                return apiPost('/api/ai/commands/run', { command: c.name, args, confirm })
                    .then(d => {
                        pushAiLog({
                            time: new Date().toLocaleTimeString('ru-RU'), command: d.command,
                            args: d.args, icon: d.icon, status: d.status,
                            output: d.output || (d.result === null || d.result === undefined ? 'OK' : String(d.result)),
                            elapsed: d.elapsed
                        });
                        say('aiLog', d.status === 'error' ? 'ошибка' : 'готово',
                            d.status === 'error' ? 'err' : 'ok');
                    })
                    .catch(e => say('aiLog', e.message, 'err'))
                    .finally(() => busy(btn, false));
            };

            if (c.danger && aiPending !== c.name) {
                aiPending = c.name;
                const msg = `${c.icon || ''} ${c.name} — нажми ещё раз, чтобы подтвердить`;
                say('aiLog', msg, 'err');
                btn.classList.add('warn');
                setTimeout(() => { if (aiPending === c.name) { aiPending = null; btn.classList.remove('warn'); } }, 5000);
                return;
            }
            aiPending = null;
            btn.classList.remove('warn');
            await send(!!c.danger);
        }

        async function clearAiLog() {
            try { await apiPost('/api/ai/commands/clear', {}); } catch (e) { /* журнал и так почистим */ }
            aiLog.length = 0;
            renderAiLog();
            $('aiSub').textContent = `${aiCmds.length} команд(ы), 0 в журнале`;
        }

        async function onMenuItem(it) {
            clearLog('menuLog');
            if (it.mode === 'interactive') {
                const field = CFG_TARGET[it.name];
                if (field) {
                    togglePanel('cfg');
                    setTimeout(() => { const el = $(field); if (el) { el.focus(); if (el.select) el.select(); } }, 60);
                    const msg = `/${it.name} задаётся в CONFIG — поле «${field.replace('cfg', '').toLowerCase()}» подсвечено`;
                    say('menuLog', msg, 'dim');
                    pushCmdLog(it.name, msg, 'dim');
                } else {
                    const msg = `/${it.name} выполняется только в терминале`;
                    say('menuLog', msg, 'warn');
                    pushCmdLog(it.name, msg, 'warn');
                }
                return;
            }
            if (it.mode === 'danger' && pendingConfirm !== it.name) {
                pendingConfirm = it.name;
                const msg = `нажми ещё раз, чтобы подтвердить /${it.name}`;
                say('menuLog', msg, 'err');
                pushCmdLog(it.name, msg, 'warn');
                setTimeout(() => { if (pendingConfirm === it.name) pendingConfirm = null; }, 4000);
                return;
            }
            pendingConfirm = null;
            say('menuLog', `выполняю /${it.name} …`, 'dim');
            try {
                const d = await apiPost('/api/menu/run', { name: it.name, confirm: true });
                say('menuLog', d.output || 'OK', d.status === 'confirm_required' ? 'warn' : 'ok');
                pushCmdLog(it.name, d.output || 'OK', d.status === 'confirm_required' ? 'warn' : 'ok');
            } catch (e) {
                say('menuLog', e.message, 'err');
                pushCmdLog(it.name, e.message, 'err');
            }
        }

        // показать активного провайдера и модель в статус-баре
        function refreshStateBadge() {
            const el = $('sbModel');
            if (!el) return;
            fetch('/api/ai/config').then(r => r.json()).then(c => {
                if (!c || c.detail) return;
                el.textContent = c.summary || `${c.provider} ${c.active_model || '—'}`;
                el.title = `Провайдер: ${c.provider_label}\nМодель: ${c.active_model}\nАдрес: ${c.base_url || '—'}`;
            }).catch(() => {});
        }

        // ─────────── горячие клавиши и клики ───────────
        window.addEventListener('keydown', e => {
            if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') { e.preventDefault(); togglePanel('cfg'); return; }
            if (e.key === 'F2') { e.preventDefault(); togglePanel('menu'); return; }
            if (e.key === 'F3') { e.preventDefault(); togglePanel('ai'); return; }
            if (e.key === 'Escape' && openPanel) { e.preventDefault(); togglePanel(openPanel); return; }
        });
        const PANEL_NAME = { cfgPanel: 'cfg', aiPanel: 'ai' };
        document.querySelectorAll('.panel').forEach(p => {
            p.addEventListener('mousedown', e => { if (e.target === p) togglePanel(PANEL_NAME[p.id] || 'menu'); });
        });

        // подтягиваем реальные команды меню для Tab-подсказок
        function loadMenuHints() {
            fetch('/api/menu').then(r => r.json()).then(d => {
                if (!d || d.detail) return;
                menuData = menuData || d;
                addCmdHints((d.groups || []).flatMap(g => g.items.map(i => '/' + i.name)));
            }).catch(() => {});
        }


        // --- 2. РЕНДЕР ГОЛОГРАММЫ --- //
        let idTimeT = null; const S_SLEEP_DELAY = 5000; let showCore = false;
        let SCE, CM, RDR, CTROLS, UNI_GRP, UPDATERS = [], sysFrame = null; 
        let engineInst = false;
        
        const CLRS = {
            Cyantific: 0x00f3ff, // Научный ярко-лазурный 
            HotGold: 0xffb700,   // Теплый сочный янтарь (Основной UI Тони)
            BurnOrange: 0xff3800,// Техногенный плазменный кроваво-красный в основе
            DiamondPt: 0xfffcf0 // Сияние
        };

        function invokeScreensaverOverlay() {
            if (!uiPrefs.three3d) return;                 // 3D выключена — оверлей не показываем
            if (showCore) return; showCore = true;
            document.getElementById('screensaver-ui').classList.add('active');
            document.activeElement?.blur();
            if (!engineInst) {
                // сначала грузим three.js, и только потом запускаем отрисовку
                ensureThree().then(ok => {
                    if (!ok || !showCore) return;
                    setupMasterGeometricHologram();
                    if (showCore) sysFrame = requestAnimationFrame(jarvisLoopProc);
                });
                return;
            }
            sysFrame = requestAnimationFrame(jarvisLoopProc);
        }

        function hideScreensaver() {
            if (!showCore) return; showCore = false;
            document.getElementById('screensaver-ui').classList.remove('active');
            if (uiPrefs.three3d) { clearTimeout(idTimeT); idTimeT = setTimeout(invokeScreensaverOverlay, S_SLEEP_DELAY); }
            if (uiPrefs.input) userInput.focus();
            if(sysFrame) cancelAnimationFrame(sysFrame); 
        }

        function checkTouchLive() { if (!showCore && uiPrefs.three3d) { clearTimeout(idTimeT); idTimeT = setTimeout(invokeScreensaverOverlay, S_SLEEP_DELAY); } }
        window.addEventListener('load', () => {
            loadUiPrefs(); applyUiPrefs(); startClock();
            setCoreState('live', 'READY');
            // мигающий блочный курсор горит только когда поле ввода в фокусе
            const bar = document.getElementById('inputContainer');
            const syncFocus = () => { if (bar) bar.classList.toggle('focused', document.activeElement === userInput); };
            userInput.addEventListener('focus', syncFocus); userInput.addEventListener('blur', syncFocus); syncFocus();
            // тап по строке промпта (и по пустому месту потока) открывает клавиатуру
            bar.addEventListener('click', () => { if (uiPrefs.input) userInput.focus(); });
            chatContainer.addEventListener('click', e => {
                if (!uiPrefs.input || e.target.closest('.play-audio-btn, button, a, input')) return;
                userInput.focus();
            });
            // рамка из псевдографики и печать баннера
            renderFrame();
            window.addEventListener('resize', renderFrame);
            typeBoot();
            if (uiPrefs.input) userInput.focus();
            loadMenuHints(); refreshStateBadge(); renderCmdLog();
            ['mousemove','mousedown','keydown','scroll','touchstart','wheel'].forEach(evt => document.addEventListener(evt, checkTouchLive, {passive: true}));
            checkTouchLive();
        });


        // СОЗДАЕМ ПОЛНУЮ МАГИЮ СЛОЖНЫХ, ЭСТЕТИЧНЫХ ПРОЦЕДУРНЫХ ФОРМ (Никаких плохих линий, только четкость!)
        function setupMasterGeometricHologram() {
            if(typeof THREE === 'undefined') return;
            const cnvDiv = document.getElementById('three-canvas-container');

            // 1. Scene & Atmosphere (Среда глубины для растворения осей по Z/Y)
            SCE = new THREE.Scene();
            SCE.fog = new THREE.FogExp2(0x050104, 0.018); 

            CM = new THREE.PerspectiveCamera(40, window.innerWidth/window.innerHeight, 0.5, 200);
            CM.position.z = 24; 

            // 2. Additive Rendering engine for HDR looks on mobile devices
            RDR = new THREE.WebGLRenderer({ antialias: true, alpha: true, powerPreference: "high-performance" });
            RDR.setPixelRatio(Math.min(window.devicePixelRatio, 2));
            RDR.setSize(window.innerWidth, window.innerHeight);
            cnvDiv.appendChild(RDR.domElement);

            // Orbit Interactor
            CTROLS = new THREE.OrbitControls(CM, RDR.domElement);
            CTROLS.enableDamping = true; CTROLS.dampingFactor = 0.035; CTROLS.enablePan = false;
            CTROLS.minDistance = 6; CTROLS.maxDistance = 60;
            window.addEventListener('resize', () => { if(CM) { CM.aspect=window.innerWidth/window.innerHeight; CM.updateProjectionMatrix(); RDR.setSize(window.innerWidth,window.innerHeight); } });

            UNI_GRP = new THREE.Group(); SCE.add(UNI_GRP);
            UPDATERS = [];
            const OvrBld = { transparent: true, blending: THREE.AdditiveBlending, depthWrite: false };

            // Материалы колец — для перекраски по состоянию ЛУЧА
            const holoRings = [];

            // ----- КОМПОНЕНТ 1: АЛМАЗНОЕ ЯДРО (взаимопроникающие октаэдр, икосаэдр и додекаэдр) -----
            const cHeart = new THREE.Group();
            
            // Внутренний сплошной циановый кристаллик, как мозг данных.
            const bMatDia = new THREE.MeshBasicMaterial({color: CLRS.Cyantific, opacity: 0.1, ...OvrBld});
            let coreDia = new THREE.Mesh(new THREE.OctahedronGeometry(1.0, 0), bMatDia);
            
            // Закованный в перекрещенный каркас Додекаэдра.
            const wGold = new THREE.LineBasicMaterial({color: CLRS.HotGold, opacity: 0.2, ...OvrBld});
            let cageDodeca1 = new THREE.LineSegments(new THREE.WireframeGeometry(new THREE.DodecahedronGeometry(2.5, 0)), wGold);
            let cageIcosa1 = new THREE.LineSegments(new THREE.WireframeGeometry(new THREE.IcosahedronGeometry(2.8, 1)), new THREE.LineBasicMaterial({color: CLRS.DiamondPt, opacity:0.12, ...OvrBld}));
            
            // Коронное огненное облако вокруг платины ядра.
            const pFGeom = new THREE.BufferGeometry();
            const pfLgth = 700; const pfArr = new Float32Array(pfLgth*3);
            for (let e=0; e<pfLgth; e++){
                let rp = 1.4 + 1.2*Math.pow(Math.random(),2);
                let xTh = 2*Math.PI*Math.random(); let yPh = Math.acos(2*Math.random()-1);
                pfArr[e*3] = rp*Math.sin(yPh)*Math.cos(xTh); pfArr[e*3+1] = rp*Math.sin(yPh)*Math.sin(xTh); pfArr[e*3+2] = rp*Math.cos(yPh);
            }
            pFGeom.setAttribute('position', new THREE.BufferAttribute(pfArr, 3));
            let coreFire = new THREE.Points(pFGeom, new THREE.PointsMaterial({color:CLRS.BurnOrange, size:0.045, ...OvrBld, opacity: 0.7}));

            cHeart.add(coreDia, cageDodeca1, cageIcosa1, coreFire);
            
            // Закинем все гео-капсулы сердца в функцию анимации через хуки userData.
            cageDodeca1.userData = { ry: 0.003, rx: 0.001 }; cageIcosa1.userData = { ry: -0.0015, rx: -0.002 }; coreDia.userData = { ry: -0.008 };
            UPDATERS.push(cageDodeca1, cageIcosa1, coreDia);
            UNI_GRP.add(cHeart);


            // ----- КОМПОНЕНТ 2: МАГИСТРАЛЬ СКОРОСТИ ГЕКСАГОНОВ (Hex Vertical Matrix Corridors) -----
            // Мы выстраиваем несколько длинных призрачных проволочных шестиугольников с обдувающимися концами для создания "Матрицы Баз Данных", уходящих вверх/вниз
            for (let idxCol=1; idxCol<=3; idxCol++){
                let radBase = 1.8 + idxCol * 0.4;
                let c_ht = 18.0 + (idxCol * 6.0); // очень высокие цилиндры
                // Строим с radiusSegments=6 = идеальный Хексагон! (Пчёлинная сота/Вычислительный шлейф).
                let hCylG = new THREE.WireframeGeometry(new THREE.CylinderGeometry(radBase, radBase, c_ht, 6, 4, true));
                // Понижаем прозрачность очень сильно (0.04), чтобы матричный эффект был тонким призраком. 
                let mtxLn = new THREE.LineSegments(hCylG, new THREE.LineBasicMaterial({color: CLRS.Cyantific, ...OvrBld, opacity:0.025 + (Math.random()*0.02)}));
                // Чутка повернуть вокруг центра:
                mtxLn.rotation.y = Math.PI / idxCol; 
                mtxLn.userData = {ry: (idxCol%2===0?-1:1)*0.002}; // Разное вращение
                UNI_GRP.add(mtxLn);
                UPDATERS.push(mtxLn);
            }


            // ----- КОМПОНЕНТ 3: ЗОНЫ РАДАРА ТЕЛЕМЕТРИИ (Рассекающие Проекционные Конусы шлема Iron Man) -----
            const bConeMats = new THREE.LineBasicMaterial({ color: CLRS.BurnOrange, opacity: 0.04, ...OvrBld });
            let upCone = new THREE.LineSegments(new THREE.WireframeGeometry(new THREE.ConeGeometry(5, 7, 36, 1, true)), bConeMats);
            let dnCone = new THREE.LineSegments(new THREE.WireframeGeometry(new THREE.ConeGeometry(5, 7, 36, 1, true)), bConeMats);
            upCone.position.y = -6.5; // опущены, так что острие смотрит кверху? Конструкция перевертышей.
            dnCone.position.y = 6.5;
            dnCone.rotation.z = Math.PI; // Свести носики друг на друга? Наоборот, базы друг к другу как рупоры!
            upCone.userData={ry:0.002}; dnCone.userData={ry:-0.002}; 
            UNI_GRP.add(upCone, dnCone);
            UPDATERS.push(upCone, dnCone);


            // ----- КОМПОНЕНТ 4: ВИРТУАЛЬНЫЕ УЗЛЫ-ИССЛЕДОВАТЕЛИ & КАРУСЕЛЬ РЕЖЕКТОРНЫХ ШКАЛ HUD -----
            // Сгенерирует элегантные системы "Приборных Панелей Джарвиса" + орбитальные капсулы на них
            function makeDashBand_HUD_Rings(radiSys, pXa, pYa, pZa, enableCapsules=false) {
                const SLevel = new THREE.Group(); SLevel.rotation.set(pXa, pYa, pZa);
                let rndLvls = 2 + Math.floor(Math.random() * 4); // число дисковых-шин связи в орбите
                
                for(let lv = 0; lv<rndLvls; lv++) {
                    let dLocal = new THREE.Group();
                    let rLvSize = radiSys + lv*(Math.random()*0.8+0.2); 
                    
                    let pieceDiv = 4 + Math.floor(Math.random()*16); 
                    for (let pc=0; pc<pieceDiv; pc++){
                        let pSliceLn = (Math.PI*2)/pieceDiv * (0.3+Math.random()*0.45); // Обрываем интерфейсные рамки на пустые пространства
                        let rgBthk = lv % 3 === 0 ? 0.015 : (0.05 + Math.random()*0.08); // Тощина
                        
                        // Цвет кольца-деления
                        let bMatCol = CLRS.HotGold; let rxr = Math.random();
                        if (rxr>0.9) bMatCol = CLRS.DiamondPt; else if (rxr>0.75) bMatCol = CLRS.BurnOrange;
                        else if (rxr>0.6 && rgBthk>0.05) bMatCol = CLRS.Cyantific;
                        
                        let M_RG = new THREE.Mesh(
                            new THREE.RingGeometry(rLvSize, rLvSize+rgBthk, 32, 1, pc * ((Math.PI*2)/pieceDiv), pSliceLn),
                            new THREE.MeshBasicMaterial({color:bMatCol, side:THREE.DoubleSide, ...OvrBld, opacity: 0.15 + (Math.random()*0.45)})
                        );
                        dLocal.add(M_RG);
                        holoRings.push(M_RG.material);
                    }
                    dLocal.userData = {rz: (Math.random()-0.5)*0.012};

                    // ---- Если это главный орбитальный экватор, повесим физические спутники-кристаллы ---- //
                    if (enableCapsules && Math.random()>0.4) {
                        let objGeomsType = new THREE.TetrahedronGeometry(0.2, 0); // Мелкие пирады информации. 
                        let dataModlCnt = 2 + Math.floor(Math.random()*4); 
                        for(let stL=0; stL < dataModlCnt; stL++){
                            let dMeshCap = new THREE.Mesh(objGeomsType, new THREE.MeshBasicMaterial({color: CLRS.Cyantific, ...OvrBld, opacity:0.85}));
                            let angleCaps = (Math.PI*2)/dataModlCnt * stL; 
                            dMeshCap.position.x = rLvSize * Math.cos(angleCaps); dMeshCap.position.y = rLvSize * Math.sin(angleCaps);
                            // Сами маленькие кусочки данных тоже будем кувыркать вокруг себя во время полета
                            dMeshCap.userData = { isHologramNode: true, spinX: Math.random()*0.02, spinY: Math.random()*0.02 }; 
                            dLocal.add(dMeshCap);
                            UPDATERS.push(dMeshCap);
                        }
                    }

                    UPDATERS.push(dLocal); SLevel.add(dLocal);
                }
                UNI_GRP.add(SLevel);
            }
            
            // Создаем многомерные разломанные панели - одна плоская/толстая орбита с Кубами/Кристаллами Информации
            makeDashBand_HUD_Rings(6.4, 0,0,0, true);
            makeDashBand_HUD_Rings(5.5, Math.PI/2,0,0);
            makeDashBand_HUD_Rings(7.1, 0, Math.PI/2.1, 0, true);
            makeDashBand_HUD_Rings(7.8, Math.PI/6, Math.PI/3.2, 0.4);


            // ----- КОМПОНЕНТ 5: ТОНКИЙ АСТРАЛЬНЫЙ ОКУТЫВАЮЩИЙ "ЗЕМНОЙ" ШАР -----
            // Сфера-сетка глобус поверх всей конструкции телеметрии. Идеальная проволочная структура без полигональной серости!
            let superSphereGeo = new THREE.WireframeGeometry(new THREE.SphereGeometry(6.6, 56, 30));
            let netGlob = new THREE.LineSegments(superSphereGeo, new THREE.LineBasicMaterial({color: CLRS.BurnOrange, ...OvrBld, opacity:0.07}));
            netGlob.userData = {rx: 0.0006, rz: 0.0002}; 
            UNI_GRP.add(netGlob); UPDATERS.push(netGlob);


            // Сохраняем ссылки на ключевые объекты — для подсветки активности ЛУЧА
            window.LUCH_HOLO = {
                coreDia, cageDodeca1, cageIcosa1, coreFire, netGlob,
                rings: holoRings, updaters: UPDATERS
            };
            applyAIState(AI_STATE);


            engineInst = true; 
        }

        // --- ДВИГАТЕЛЬ СЦЕНЫ, ПЕРЕМЕШИВАЮЩИЙ ГЕОМЕТРИЮ (Вращаем всё по заложенным userdata векторам)
        function jarvisLoopProc() {
            if (!showCore) return; 
            sysFrame = requestAnimationFrame(jarvisLoopProc);
            
            // Базовый покачивающий и вечный параллакс самого ядра Вселенной! 
            if(UNI_GRP) { 
                UNI_GRP.rotation.y += 0.00045; 
                UNI_GRP.rotation.z += 0.00018; 
            }
            
            for(let itms = 0; itms < UPDATERS.length; itms++) {
                let eMsh = UPDATERS[itms]; let udta = eMsh.userData;
                // Базовые облеты компонентов. (Если есть параметры вращения в user data)
                if(udta.rx !== undefined) eMsh.rotation.x += udta.rx;
                if(udta.ry !== undefined) eMsh.rotation.y += udta.ry;
                if(udta.rz !== undefined) eMsh.rotation.z += udta.rz;

                // Если это наши спутники-Данные. Кувыркаем их самих, пока их контейнер DLocal кружится вокруг всей базы!
                if(udta.isHologramNode) { eMsh.rotation.x += udta.spinX; eMsh.rotation.y += udta.spinY; }
            }

            if (CTROLS) CTROLS.update();
            if (RDR && SCE && CM) RDR.render(SCE, CM);
        }

        // --- 3. BACKGROUND NETWORK PINGS (Для Таймеров Сервера Консоли) --- 
        setInterval(async () => {
            try {
                const rqs = await fetch('/api/alerts'); if(!rqs.ok) return;
                const payl = await rqs.json();
                if (payl.alerts?.length > 0) {
                    payl.alerts.forEach(async (alItem) => {
                        let sysTbx = addMessage('💠 INCOMING DATALINK: ' + alItem.description, 'ai');
                        sysTbx.style.borderColor = "var(--neon-pink)"; sysTbx.style.boxShadow = "var(--shadow-glow-pink)";
                        try {
                            const bbb = await (await fetch('/api/alert_audio?t=' + Date.now())).blob();
                            let wpAud = new Audio(URL.createObjectURL(bbb));
                            wpAud.play().catch(()=>{
                                let bcBt = document.createElement('button'); bcBt.className = 'play-audio-btn';
                                bcBt.style.color = "var(--neon-pink)"; bcBt.style.borderColor="var(--neon-pink)";
                                bcBt.innerHTML = '🔊 ACTIVATE LINK SIGNAL'; bcBt.onclick = ()=>wpAud.play();
                                sysTbx.appendChild(document.createElement('br')); sysTbx.appendChild(bcBt);
                            });
                        } catch (er) {}
                    });
                }
            } catch(e) {}
        }, 2500); 


        // --- 4. ГЕОЛОКАЦИЯ / НАВИГАТОР (Leaflet + OpenStreetMap) ---
        const mapPanel = document.getElementById('mapPanel');
        const mapToggleBtn = document.getElementById('mapToggle');
        const mapStatus = document.getElementById('mapStatus');
        const navDestInput = document.getElementById('navDest');
        const navBuildBtn = document.getElementById('navBuildBtn');

        let map = null, userMarker = null, userCircle = null, routeLayer = null, destMarker = null;
        let geoWatchId = null, lastSentPos = null, destPos = null, userInteracted = false;

        function setMapStatus(text, ok) {
            mapStatus.textContent = text;
            mapStatus.dataset.ok = ok ? '1' : '0';
        }

        function initMap() {
            if (map) return;
            map = L.map('map').setView([55.75, 37.61], 13);
            L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
                attribution: '&copy; OpenStreetMap',
                maxZoom: 19
            }).addTo(map);
            map.on('click', onMapClick);
            // Если пользователь двигает/зумит карту сам — не притягиваем её обратно к маркеру
            map.on('dragstart', () => userInteracted = true);
            map.on('zoomstart', () => userInteracted = true);
        }

        function toggleMap() {
            const open = mapPanel.classList.toggle('open');
            mapToggleBtn.classList.toggle('on', open);
            if (open) {
                initMap();
                setTimeout(() => { if (map) map.invalidateSize(); }, 100);
                // При открытии один раз центрируем на текущей позиции
                if (userMarker && lastKnownPos && !userInteracted) {
                    map.setView([lastKnownPos.lat, lastKnownPos.lng], Math.max(map.getZoom(), 16));
                }
                startGeolocation();
            } else {
                stopGeolocation();
            }
        }

        let lastKnownPos = null;

        function startGeolocation() {
            if (!navigator.geolocation) { setMapStatus('Геолокация не поддерживается браузером', false); return; }
            setMapStatus('Запрос геолокации...', false);
            if (geoWatchId !== null) return;
            geoWatchId = navigator.geolocation.watchPosition(
                onGeoSuccess, onGeoError,
                { enableHighAccuracy: true, timeout: 15000, maximumAge: 5000 }
            );
        }

        function stopGeolocation() {
            if (geoWatchId !== null) { navigator.geolocation.clearWatch(geoWatchId); geoWatchId = null; }
        }

        function centerOnUser() {
            if (!map) return;
            if (lastKnownPos) { map.setView([lastKnownPos.lat, lastKnownPos.lng], Math.max(map.getZoom(), 16)); }
            userInteracted = false;   // вернуть автоматическое следование
        }

        function onGeoSuccess(pos) {
            const la = pos.coords.latitude, lo = pos.coords.longitude, acc = pos.coords.accuracy;
            setMapStatus(`GPS: ${la.toFixed(5)}, ${lo.toFixed(5)} ±${Math.round(acc)} м`, true);
            lastKnownPos = { lat: la, lng: lo, accuracy: acc, source: 'browser' };
            updateMarker(lastKnownPos);
            sendGeolocationToServer(la, lo, acc);
            addHistoryPoint({ lat: la, lng: lo });
            // Если ИИ запрашивал свежую позицию — погасили запрос после первой отправки
            if (locRequestPending) {
                locRequestPending = false;
                fetch('/api/location/request/resolve', { method: 'POST' }).catch(()=>{});
            }
        }

        // ИИ может запросить координаты клиента (команда request_location) —
        // проверяем флаг на сервере и при необходимости включаем геолокацию
        let locRequestPending = false;
        setInterval(async () => {
            try {
                const r = await fetch('/api/location/request');
                const d = await r.json();
                if (d && d.pending) {
                    locRequestPending = true;
                    lastSentPos = null;                    // снять «тюльпан» срочности
                    if (geoWatchId === null) startGeolocation();
                }
            } catch (e) { /* сервер недоступен */ }
        }, 3000);

        // Отправляем координаты на сервер (не чаще раза в 8 сек)
        function sendGeolocationToServer(lat, lng, accuracy) {
            const now = Date.now();
            if (lastSentPos && (now - lastSentPos) < 8000) return;
            lastSentPos = now;
            fetch('/api/location', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ lat, lng, accuracy, source: 'browser' })
            }).catch(()=>{});
        }

        function onGeoError(err) {
            setMapStatus('Геолокация недоступна: ' + err.message, false);
        }

        function onMapClick(e) {
            destPos = { lat: e.latlng.lat, lng: e.latlng.lng };
            e.target.openPopup();
        }

        function updateMarker(pos) {
            const firstFix = !userMarker;
            initMap();
            if (userMarker) { userMarker.setLatLng([pos.lat, pos.lng]); }
            else { userMarker = L.circleMarker([pos.lat, pos.lng], {
                radius: 8, color: '#2ecc71', weight: 2, fillColor: '#2ecc71', fillOpacity: 0.5 }).addTo(map);
                userMarker.bindPopup('Текущая позиция');
            }
            if (userCircle) userCircle.setLatLng([pos.lat, pos.lng]).setRadius(pos.accuracy || 10);
            else userCircle = L.circle([pos.lat, pos.lng], { radius: pos.accuracy || 10, color: '#2ecc71', opacity: 0.25 }).addTo(map);
            // Центрируем карту только при первом «фиксе» и если юзер сам карту не двигал
            if (firstFix && !userInteracted) {
                map.setView([pos.lat, pos.lng], Math.max(map.getZoom(), 16));
            }
        }

        // История перемещений (слой с лимитом на 100 точек)
        function addHistoryPoint(pos) {
            if (!map) return;
            if (!map.historyLayer) map.historyLayer = L.layerGroup().addTo(map);
            const m = L.circleMarker([pos.lat, pos.lng], { radius: 3, color: '#48c9b0', fillColor: '#48c9b0', fillOpacity: 0.5 });
            m.addTo(map.historyLayer);
            while (map.historyLayer.getLayers().length > 100) map.historyLayer.removeLayer(map.historyLayer.getLayers()[0]);
        }

        // Построение маршрута + голосовой режим навигатора (сервер: geocode + OSRM + steps)
        let navMode = false;

        const navStopBtn = document.getElementById('navStopBtn');
        const navManeuverBanner = document.getElementById('navManeuverBanner');
        const navManeuverText = document.getElementById('navManeuverText');
        const navManeuverDist = document.getElementById('navManeuverDist');

        function showManeuver(man) {
            if (!man) { navManeuverBanner.style.display = 'none'; return; }
            navManeuverText.textContent = man.text || 'Двигайтесь по маршруту';
            navManeuverDist.textContent = man.distance_m != null ? `${man.distance_m} м` : '';
            navManeuverBanner.style.display = 'flex';
            // Эффект обновления баннера
            navManeuverBanner.style.animation = 'none';
            void navManeuverBanner.offsetWidth;
            navManeuverBanner.style.animation = '';
        }

        function setNavModeUI(on) {
            navMode = on;
            navStopBtn.style.display = on ? '' : 'none';
            navBuildBtn.classList.toggle('active', on);
        }

        async function buildRoute() {
            const dest = navDestInput.value.trim();
            if (!dest) { setMapStatus('Укажи, куда едем (адрес или фраза)', false); return; }
            if (!map) initMap();
            try {
                const res = await fetch('/api/navigator/start', {
                    method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ destination: dest })
                });
                const d = await res.json();
                const route = d && d.data;
                if (!route || route.status !== 'navigation_started') {
                    setMapStatus('Не удалось построить маршрут: ' + ((route && route.message) || 'неизвестная ошибка'), false);
                    return;
                }
                destPos = route.destination_coords;
                initMap();
                destMarker = destMarker || L.marker([destPos.lat, destPos.lng]).addTo(map);
                destMarker.setLatLng([destPos.lat, destPos.lng]).bindPopup(route.destination).openPopup();
                if (routeLayer) map.removeLayer(routeLayer);
                const coords = (route.geometry || []);
                if (coords.length) {
                    routeLayer = L.polyline(coords.filter(c => Array.isArray(c) && c.length >= 2),
                        { color: '#f1c40f', weight: 5, opacity: 0.7 }).addTo(map);
                    map.fitBounds(routeLayer.getBounds().pad(0.1));
                } else {
                    const f = route.from || { lat: 0, lng: 0 };
                    routeLayer = L.polyline([[f.lat, f.lng], [destPos.lat, destPos.lng]],
                        { color: '#f1c40f', weight: 3, dashArray: '6,4' }).addTo(map);
                    setMapStatus('OSRM недоступен — прямая линия до цели', false);
                }
                setMapStatus(`Маршрут: ${route.distance_km} км · ~${Math.round(route.duration_min)} мин`, true);
                showManeuver(route.next_maneuver);
                setNavModeUI(true);
                startGeolocation();
            } catch (e) {
                setMapStatus('Ошибка построения маршрута: ' + e.message, false);
            }
        }

        async function stopNavMode() {
            try {
                await fetch('/api/navigator/stop', { method: 'POST' });
            } catch (e) { /* сервер может быть недоступен */ }
            setNavModeUI(false);
            showManeuver(null);
            setMapStatus('Навигация остановлена', true);
        }

        // Опрос состояния навигатора: обновляем баннер манёвра и позицию
        setInterval(async () => {
            if (!navMode) return;
            try {
                const res = await fetch('/api/navigator/status');
                const d = await res.json();
                if (!d || !d.active) { setNavModeUI(false); showManeuver(null); return; }
                if (d.next_maneuver) showManeuver(d.next_maneuver);
                if (d.current_location && d.current_location.lat != null && lastKnownPos) {
                    const mlat = d.current_location.lat, mlng = d.current_location.lng;
                    const gps = lastKnownPos;
                    if (Math.abs(mlat - gps.lat) > 0.000001 || Math.abs(mlng - gps.lng) > 0.000001) {
                        updateMarker({ lat: mlat, lng: mlng, accuracy: gps.accuracy, source: 'server' });
                    }
                }
            } catch (e) { /* сервер недоступен */ }
        }, 2000);
        

        // --- 5. АКТИВНОСТЬ ЛУЧА НА 3D-ЯДРЕ (по состоянию с сервера) ---
        const holoStatusEl = document.getElementById('holoStatus');
        let AI_STATE = 'idle';

        // Каждое состояние: основной цвет ядра, цвет «огня», скорость, яркость, подпись, HTML-цвет для статуса
        const STATE_THEME = {
            idle:      { color: 0x00f3ff, fire: 0xff3800, speed: 1.0, intensity: 0.3, label: 'ПОКОЙ',      css: '#4df8ff' },
            listening: { color: 0xf5c2e7, fire: 0xffb700, speed: 1.2, intensity: 0.45, label: 'СЛУШАЕТ',    css: '#f5c2e7' },
            thinking:  { color: 0x7a5cff, fire: 0x00f3ff, speed: 2.4, intensity: 0.7,  label: 'ДУМАЕТ',     css: '#a78bfa' },
            speaking:  { color: 0xffb700, fire: 0xff3800, speed: 1.7, intensity: 0.55, label: 'ГОВОРИТ',    css: '#fab387' },
            command:   { color: 0x2ecc71, fire: 0xf1c40f, speed: 3.0, intensity: 0.95, label: 'ВЫПОЛНЯЕТ КОМАНДУ', css: '#a6e3a1' },
            error:     { color: 0xf38ba8, fire: 0xff3800, speed: 1.4, intensity: 0.7,  label: 'ОШИБКА',     css: '#f38ba8' },
        };

        function applyAIState(state) {
            AI_STATE = STATE_THEME[state] ? state : 'idle';
            const t = STATE_THEME[AI_STATE];
            if (holoStatusEl) {
                holoStatusEl.textContent = 'РЕЖИМ: ' + t.label;
                holoStatusEl.style.color = t.css;
                holoStatusEl.style.textShadow = `0 0 22px ${t.css}`;
            }
            const h = window.LUCH_HOLO;
            if (!h) return;

            if (h.coreDia) {
                h.coreDia.material.color.setHex(t.color);
                h.coreDia.material.opacity = 0.08 + 0.14 * t.intensity;
            }
            if (h.coreFire) {
                h.coreFire.material.color.setHex(t.fire);
                h.coreFire.material.opacity = 0.25 + 0.55 * t.intensity;
                h.coreFire.material.size = 0.03 + 0.035 * t.intensity;
            }
            // Оболочки-каркасы
            if (h.cageDodeca1) { h.cageDodeca1.material.color.setHex(t.color); h.cageDodeca1.material.opacity = 0.15 + 0.25 * t.intensity; }
            if (h.cageIcosa1)  { h.cageIcosa1.material.color.setHex(t.color); h.cageIcosa1.material.opacity = 0.10 + 0.18 * t.intensity; }
            if (h.netGlob)     { h.netGlob.material.color.setHex(t.color);     h.netGlob.material.opacity = 0.05 + 0.10 * t.intensity; }
            // Кольца-шкалы
            if (h.rings) h.rings.forEach(m => {
                m.color.setHex(t.color);
                m.opacity = (0.12 + 0.4 * t.intensity) * (0.5 + Math.random() * 0.5);
            });
            // Скорость вращения: сохраняем базу при первом применении и умножаем на speed
            h.updaters.forEach(u => {
                const ud = u.userData;
                if (!ud) return;
                if (ud.__base === undefined) {
                    ud.__base = { rx: ud.rx || 0, ry: ud.ry || 0, rz: ud.rz || 0 };
                }
                ud.rx = ud.__base.rx * t.speed;
                ud.ry = ud.__base.ry * t.speed;
                ud.rz = ud.__base.rz * t.speed;
            });
        }

        // Опрос состояния LУЧА с сервера (раз в секунду)
        setInterval(async () => {
            try {
                const r = await fetch('/api/state');
                const d = await r.json();
                if (d && d.state) applyAIState(d.state);
            } catch (e) { /* сервер может быть недоступен */ }
        }, 1000);

        setInterval(refreshStateBadge, 5000);

        // пока панель AI CMD открыта — подтягиваем команды, которые выполнил агент в консоли
        setInterval(() => {
            if (openPanel !== 'ai') return;
            apiGet('/api/ai/commands').then(d => {
                const hist = d.history || [];
                if (hist.length === aiLog.length && hist.length
                    && hist[hist.length - 1].seq === aiLog[aiLog.length - 1].seq) return;
                aiLog.length = 0; hist.slice(-30).forEach(h => aiLog.push(h));
                renderAiLog();
                $('aiSub').textContent = `${aiCmds.length} команд(ы), ${aiLog.length} в журнале`;
            }).catch(() => {});
        }, 3000);

