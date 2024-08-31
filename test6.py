import time
from typing import List, Tuple
from whisperx.vad import load_vad_model, merge_chunks
from whisperx.audio import load_audio, log_mel_spectrogram  # Import log_mel_spectrogram to convert audio to mel spectrogram
import faster_whisper
import numpy as np
import ctranslate2

SAMPLE_RATE = 16000

class WhisperModel(faster_whisper.WhisperModel):
    '''
    FasterWhisperModel provides batched inference for faster-whisper.
    Currently only works in non-timestamp mode and fixed prompt for all samples in batch.
    '''

    def generate_segment_batched(self, features: np.ndarray, tokenizer: faster_whisper.tokenizer.Tokenizer, options: faster_whisper.transcribe.TranscriptionOptions, encoder_output = None):
        batch_size = features.shape[0]
        all_tokens = []
        prompt_reset_since = 0
        if options.initial_prompt is not None:
            initial_prompt = " " + options.initial_prompt.strip()
            initial_prompt_tokens = tokenizer.encode(initial_prompt)
            all_tokens.extend(initial_prompt_tokens)
        previous_tokens = all_tokens[prompt_reset_since:]
        prompt = self.get_prompt(
            tokenizer,
            previous_tokens,
            without_timestamps=options.without_timestamps,
            prefix=options.prefix,
        )

        encoder_output = self.encode(features)

        max_initial_timestamp_index = int(
            round(options.max_initial_timestamp / self.time_precision)
        )

        result = self.model.generate(
                encoder_output,
                [prompt] * batch_size,
                beam_size=options.beam_size,
                patience=options.patience,
                length_penalty=options.length_penalty,
                max_length=self.max_length,
                suppress_blank=options.suppress_blank,
                suppress_tokens=options.suppress_tokens,
            )

        tokens_batch = [x.sequences_ids[0] for x in result]

        def decode_batch(tokens: List[List[int]]) -> str:
            res = []
            for tk in tokens:
                res.append([token for token in tk if token < tokenizer.eot])
            return tokenizer.tokenizer.decode_batch(res)

        text = decode_batch(tokens_batch)

        return text

    def encode(self, features: np.ndarray) -> ctranslate2.StorageView:
        # When the model is running on multiple GPUs, the encoder output should be moved
        # to the CPU since we don't know which GPU will handle the next job.
        to_cpu = self.model.device == "cuda" and len(self.model.device_index) > 1
        # unsqueeze if batch size = 1
        if len(features.shape) == 2:
            features = np.expand_dims(features, 0)
        features = faster_whisper.transcribe.get_ctranslate2_storage(features)

        return self.model.encode(features, to_cpu=to_cpu)
    
    def detect_language(self, audio: np.ndarray, sample_rate: int, chunk_length: int) -> Tuple[str, float]:
        n_samples = chunk_length * sample_rate  # 480000 samples in a 30-second chunk
        
        # Set number of mel filters (n_mels) to 128, as expected by the model
        model_n_mels = 128
        
        # Compute the log mel spectrogram for the input audio with the correct number of mel filters
        segment = log_mel_spectrogram(
            audio[:n_samples],
            n_mels=model_n_mels,
            padding=0 if audio.shape[0] >= n_samples else n_samples - audio.shape[0]
        )
        
        # Ensure the segment is a NumPy array
        if not isinstance(segment, np.ndarray):
            segment = np.array(segment)

        # Add a batch dimension to make it 3D: [batch_size, num_mels, num_frames]
        segment = np.expand_dims(segment, axis=0)

        # Convert the spectrogram to a ctranslate2.StorageView
        segment_storage_view = ctranslate2.StorageView.from_array(segment.astype(np.float32))

        # Encode the segment using the model's encoder
        encoder_output = self.model.encode(segment_storage_view, to_cpu=False)
        
        # Detect language from the encoder output
        results = self.model.detect_language(encoder_output)
        language_token, language_probability = results[0][0]
        language = language_token[2:-2]
        
        print(f"Detected language: {language} ({language_probability:.2f})")
        return language, language_probability


# Schritt 1: VAD-Modell laden und konfigurieren
device = "cuda"  # oder "cpu" je nach Verfügbarkeit
vad_model = load_vad_model(device=device)

# Schritt 2: Whisper-Modell laden und konfigurieren
model_size = "large-v3"
model = WhisperModel(model_size, device=device, compute_type="float16")

start = time.time()

# Eingabe-Audiodatei
audio_file_path = "audio/audio.mp3"
audio_file = {"uri": "audio_sample", "audio": audio_file_path}

# Schritt 3: Sprachaktivitätserkennung durchführen
vad_result = vad_model.apply(audio_file)
# Zusammenführung von VAD-Segmenten, falls notwendig
merged_segments = merge_chunks(vad_result, chunk_size=10.0)  # Optional: Passen Sie die chunk_size an

# Extrahiere das Audio-Segment basierend auf den VAD-Ergebnissen
segment_audio = load_audio(audio_file_path, SAMPLE_RATE)  # Laden Sie die gesamte Audiodatei

# Detect language with confidence threshold
language, confidence = model.detect_language(segment_audio, SAMPLE_RATE, 30)

# Tokenizer für die Transkription initialisieren
task = "transcribe"
tokenizer = faster_whisper.tokenizer.Tokenizer(model.hf_tokenizer, model.model.is_multilingual, task=task, language=language)

default_asr_options = {
    "beam_size": 5,
    "best_of": 1,
    "patience": 1.0,
    "length_penalty": 1.0,
    "repetition_penalty": 1.0,
    "no_repeat_ngram_size": 0,
    "temperatures": [0.0, 0.2, 0.4, 0.6, 0.8, 1.0],
    "compression_ratio_threshold": 2.4,
    "log_prob_threshold": -1.0,
    "no_speech_threshold": 0.6,
    "condition_on_previous_text": False,
    "prompt_reset_on_temperature": 0.5,
    "initial_prompt": None,
    "prefix": None,
    "suppress_blank": True,
    "suppress_tokens": None,
    "without_timestamps": True,
    "max_initial_timestamp": 0.0,
    "word_timestamps": False,
    "prepend_punctuations": "\"'“¿([{-",
    "append_punctuations": "\"'.。,，!！?？:：”)]}、",
    "max_new_tokens": 50,
    "clip_timestamps": 60,
    "hallucination_silence_threshold": 0.5,
    "hotwords": None,
}

asr_options = faster_whisper.transcribe.TranscriptionOptions(**default_asr_options)

# Batch-Größe festlegen
batch_size = 32  # Beispielwert; kann angepasst werden

# Schritt 4: Transkription der VAD-basierten Sprachsegmente in Batches
batched_segments = []
max_length = 0

# Berechnen Sie die maximale Länge der Segmente
for idx, segment in enumerate(merged_segments):
    start_time = segment['start']
    end_time = segment['end']

    # Extrahiere nur das relevante Segment basierend auf den Start- und Endzeiten
    start_sample = int(start_time * SAMPLE_RATE)
    end_sample = int(end_time * SAMPLE_RATE)
    audio_segment = segment_audio[start_sample:end_sample]

    # Berechne die maximale Länge der Segmente
    max_length = max(max_length, len(audio_segment))
    batched_segments.append(audio_segment)

    # Transkribiere die Segmente in Batches
    if len(batched_segments) == batch_size or idx == len(merged_segments) - 1:
        # Konvertiere die Segmente in Mel-Spektrogramme und padding alle Segmente auf die gleiche Länge
        mel_segments = [log_mel_spectrogram(seg, padding=0, n_mels=128) for seg in batched_segments]

        # Finden Sie die maximale Anzahl von Frames (Zeitschritte) in den Mel-Spektrogrammen
        max_frames = max(mel.shape[1] for mel in mel_segments)

        # Pad jedes Mel-Spektrogramm auf die maximale Frame-Länge
        padded_mel_segments = [np.pad(mel, ((0, 0), (0, max_frames - mel.shape[1])), mode='constant') for mel in mel_segments]

        # Konvertiere die gepaddeten Mel-Segmente in ein numpy-Array
        mel_segments_array = np.array(padded_mel_segments, dtype=np.float32)  # Verwenden Sie float32 für Kompatibilität

        # Verwende die generate_segment_batched Methode zur Transkription
        transcriptions = model.generate_segment_batched(
            features=mel_segments_array,
            tokenizer=tokenizer,  # Verwenden Sie den richtigen Tokenizer
            options=asr_options,
        )

        # Ausgabe der Ergebnisse
        for i, text in enumerate(transcriptions):
            print(f"Batch {i + 1}: {text}")

        # Leere die batched_segments für den nächsten Batch
        batched_segments = []
        max_length = 0  # Reset the maximum length for the next batch

end = time.time()
print(f"Time taken: {end - start} seconds")