package proekt.luch.app

import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder
import android.util.Base64
import java.io.ByteArrayOutputStream
import java.util.concurrent.Executors
import kotlin.math.max
import kotlin.math.min

/**
 * Постоянное слушание микрофона с автоматической нарезкой фраз.
 *
 * Раньше запись шла по принципу голосовых сообщений: нажал, записал, отправил.
 * Здесь микрофон открыт всё время, а фраза выделяется сама: начало — по
 * появлению звука, конец — по паузе в речи. Каждая законченная фраза уходит
 * на сервер, а тот уже решает по триггер-слово и по голосу владельца, отвечать
 * или промолчать.
 */
class VoiceListener(private val onPhrase: (String) -> Unit) {

    @Volatile private var running = false
    private var thread: Thread? = null

    // Отправка не должна жить на потоке записи. Пока ИИ думает (а он думает
    // секунды, иногда до минуты), микрофон всё равно должен писаться — иначе
    // буфер AudioRecord переполняется и всё сказанное за это время теряется.
    // Поэтому фразы складываются в короткую очередь и уходят отдельным потоком.
    private val pending = ArrayDeque<String>()
    private val sender = Executors.newSingleThreadExecutor { r ->
        Thread(r, "luch-voice-send").apply { isDaemon = true }
    }
    @Volatile private var sending = false

    companion object {
        /** Сколько фраз держим, пока сервер думает. */
        const val MAX_PENDING = 3
        const val SAMPLE_RATE = 16000
        private const val FRAME_MS = 20
        private const val MIN_SPEECH_MS = 450      // короче — считаем шумом
        private const val END_SILENCE_MS = 1100    // столько тишины завершает фразу
        private const val MAX_PHRASE_MS = 25000
        private const val PRE_ROLL_MS = 300        // захватить начало речи
        private const val TAIL_MS = 350            // и её конец
        private const val COOLDOWN_MS = 700        // пауза после отправки
        private const val ABS_FLOOR = 380          // порог тишины при нормализации
    }

    fun isRunning(): Boolean = running

    fun start() {
        if (running) return
        running = true
        thread = Thread({ loop() }, "luch-voice").apply { start() }
    }

    fun stop() {
        synchronized(pending) { pending.clear() }
        running = false
        thread?.join(1200)
        thread = null
    }

    private fun loop() {
        val minBuf = AudioRecord.getMinBufferSize(
            SAMPLE_RATE, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT)
        if (minBuf <= 0) return
        val frameSamples = SAMPLE_RATE / 1000 * FRAME_MS
        val rec = try {
            @Suppress("DEPRECATION")
            AudioRecord(
                // VOICE_RECOGNITION — меньше автоматической обработки, лучше для распознавания
                MediaRecorder.AudioSource.VOICE_RECOGNITION, SAMPLE_RATE,
                AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT,
                max(minBuf, frameSamples * 4))
        } catch (e: Exception) {
            running = false
            return
        }
        if (rec.state != AudioRecord.STATE_INITIALIZED) {
            rec.release()
            running = false
            return
        }

        val buf = ShortArray(frameSamples)
        val preRoll = ArrayDeque<ShortArray>()
        val phrase = ByteArrayOutputStream()
        var noise = 400f          // текущий уровень фонового шума
        var speechMs = 0          // сколько уже звука в текущей фразе
        var silenceMs = 0         // сколько тишины подряд
        var collecting = false
        var cooldownMs = 0
        val framesPerRoll = max(1, PRE_ROLL_MS / FRAME_MS)
        val tailFrames = max(1, TAIL_MS / FRAME_MS)

        try {
            rec.startRecording()
            while (running) {
                val read = rec.read(buf, 0, frameSamples)
                if (read <= 0) continue

                // энергия сигнала
                var sum = 0.0
                for (i in 0 until read) {
                    val v = buf[i].toInt()
                    sum += (v * v).toDouble()
                }
                val rms = kotlin.math.sqrt(sum / read).toFloat()
                if (cooldownMs > 0) {
                    cooldownMs -= FRAME_MS
                    continue
                }
                // шум подстраивается только по тихим кадрам
                if (rms < noise * 2.5f) noise = 0.995f * noise + 0.005f * rms
                val threshold = max(noise * 3.2f, ABS_FLOOR.toFloat())
                val loud = rms > threshold

                if (!loud) {
                    silenceMs += FRAME_MS
                    if (collecting) {
                        append(phrase, buf, read)
                        if (silenceMs >= END_SILENCE_MS) {
                            // добавляем немного тишины в конец — так речь лучше угадывается
                            repeat(tailFrames) { appendSilence(phrase) }
                            if (speechMs >= MIN_SPEECH_MS) send(phrase)
                            collecting = false; speechMs = 0
                            preRoll.clear(); cooldownMs = COOLDOWN_MS
                            continue
                        }
                    } else {
                        preRoll.add(buf.copyOf(read))
                        while (preRoll.size > framesPerRoll) preRoll.removeFirst()
                    }
                    continue
                }

                // кадр с речью
                silenceMs = 0
                if (!collecting) {
                    collecting = true; speechMs = 0
                    for (f in preRoll) append(phrase, f, f.size)
                    preRoll.clear()
                }
                append(phrase, buf, read)
                speechMs += FRAME_MS
                if (speechMs >= MAX_PHRASE_MS) {
                    send(phrase)
                    collecting = false; speechMs = 0
                    preRoll.clear(); cooldownMs = COOLDOWN_MS
                }
            }
        } catch (e: Exception) {
            // микрофон занят или доступ пропал — просто выходим
        } finally {
            runCatching { if (rec.recordingState == AudioRecord.RECORDSTATE_RECORDING) rec.stop() }
            runCatching { rec.release() }
        }
    }

    private fun append(out: ByteArrayOutputStream, s: ShortArray, n: Int) {
        for (i in 0 until n) {
            val v = s[i].toInt()
            out.write(v and 0xFF)
            out.write((v shr 8) and 0xFF)
        }
    }

    private fun appendSilence(out: ByteArrayOutputStream) {
        repeat(SAMPLE_RATE / 10) {            // 100 мс тишины
            out.write(0); out.write(0)
        }
    }

    private fun send(pcm: ByteArrayOutputStream) {
        val raw = pcm.toByteArray()
        if (raw.size < SAMPLE_RATE) return     // меньше половины секунды — мусор
        val wav = wav(raw, SAMPLE_RATE)
        val b64 = Base64.encodeToString(wav, Base64.NO_WRAP)
        pcm.reset()
        synchronized(pending) {
            // Ответ ИИ может идти дольше, чем человек говорит. Держим только
            // последние фразы: отвечать на то, что было пять минут назад,
            // хуже, чем промолчать.
            while (pending.size >= MAX_PENDING) pending.removeFirst()
            pending.add(b64)
        }
        pump()
    }

    private fun pump() {
        if (sending) return
        sending = true
        sender.execute {
            while (true) {
                val b64 = synchronized(pending) { pending.removeFirstOrNull() } ?: break
                try {
                    onPhrase(b64)
                } catch (e: Exception) {
                    android.util.Log.w("LuchVoice", "не отправил фразу", e)
                }
            }
            sending = false
        }
    }

    /** Минимальный WAV-контейнер: сервер на своём ffmpeg приводит его к нужному виду. */
    private fun wav(pcm: ByteArray, rate: Int): ByteArray {
        val dataLen = pcm.size
        val out = ByteArray(44 + dataLen)
        val byteRate = rate * 2
        fun str(off: Int, s: String) {
            for (i in s.indices) out[off + i] = s[i].code.toByte()
        }
        fun i32(off: Int, v: Int) {
            out[off] = (v and 0xFF).toByte()
            out[off + 1] = ((v shr 8) and 0xFF).toByte()
            out[off + 2] = ((v shr 16) and 0xFF).toByte()
            out[off + 3] = ((v shr 24) and 0xFF).toByte()
        }
        fun i16(off: Int, v: Int) {
            out[off] = (v and 0xFF).toByte()
            out[off + 1] = ((v shr 8) and 0xFF).toByte()
        }
        str(0, "RIFF"); i32(4, 36 + dataLen); str(8, "WAVE")
        str(12, "fmt "); i32(16, 16); i16(20, 1); i16(22, 1)
        i32(24, rate); i32(28, byteRate); i16(32, 2); i16(34, 16)
        str(36, "data"); i32(40, dataLen)
        System.arraycopy(pcm, 0, out, 44, dataLen)
        return out
    }
}