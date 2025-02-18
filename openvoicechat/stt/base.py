from .utils import record_user, record_interruption, record_user_stream
from .vad import VoiceActivityDetection
import re
import numpy as np
from threading import Thread
from queue import Queue
import os
from dotenv import load_dotenv

load_dotenv()


class BaseEar:
    def __init__(
        self,
        silence_seconds=2,
        not_interrupt_words=None,
        listener=None,
        stream=False,
        listen_interruptions=True,
        logger=None,
    ):
        """
        Initializes the BaseEar class.

        :param silence_seconds: Number of seconds of silence to detect. Defaults to 2.
        :type silence_seconds: float, optional
        :param not_interrupt_words: List of words that should not be considered as interruptions.
        :type not_interrupt_words: list, optional
        :param listener: Listener object to receive the audio from. Defaults to None.
        :type listener: object, optional
        :param stream: Flag indicating whether to stream the audio or process it as a whole. Defaults to False.
        :type stream: bool, optional
        :param listen_interruptions: Flag indicating whether to listen for interruptions. Defaults to True.
        :type listen_interruptions: bool, optional
        """

        if not_interrupt_words is None:
            not_interrupt_words = [
                "you",
                "yes",
                "yeah",
                "hmm",
            ]  # you because whisper says "you" in silence
        self.silence_seconds = silence_seconds
        self.not_interrupt_words = not_interrupt_words
        self.vad = VoiceActivityDetection()
        self.listener = listener
        self.stream = stream
        self.listen_interruptions = listen_interruptions
        self.logger = logger

    def transcribe(self, input_audio: np.ndarray) -> str:
        """
        Given an audio input, return the transcription
        :param input_audio:
        :return: transcription
        """
        raise NotImplementedError("This method should be implemented by the subclass")

    def transcribe_stream(self, audio_queue: Queue, transcription_queue: Queue):
        """
        :param audio_queue: Queue containing audio chunks from pyaudio stream
        :param transcription_queue: Queue to put transcriptions
        """
        raise NotImplementedError("This method should be implemented by the subclass")

    def _sim_transcribe_stream(self, input_audio: np.ndarray) -> str:
        """
        Simulates the transcribe stream using a single audio input
        :param input_audio: fp32 numpy array of the audio
        :return: transcription
        """
        audio_queue = Queue()
        transcription_queue = Queue()

        input_buffer = (input_audio * (1 << 15)).astype(np.int16).tobytes()
        audio_queue.put(input_buffer)
        audio_queue.put(None)
        transcription_thread = Thread(
            target=self.transcribe_stream, args=(audio_queue, transcription_queue)
        )
        transcription_thread.start()
        transcription_thread.join()
        text = ""
        while True:
            _ = transcription_queue.get()
            if _ is None:
                break
            text += _ + " "
        return text

    def _log_event(self, event: str, details: str, further: str = ""):
        if self.logger:
            self.logger.info(
                event, extra={"details": details, "further": f'"{further}"'}
            )

    def _listen(self) -> str:
        """
        records audio using record_user and returns its transcription
        :return: transcription
        """
        import pysbd

        seg = pysbd.Segmenter(language="en", clean=False)

        sentence_finished = False
        first = True
        audio = np.zeros(0, dtype=np.float32)
        n = 2  # number of times to see if the sentence ends
        while not sentence_finished and n > 0:

            new_audio = record_user(
                self.silence_seconds,
                self.vad,
                self.listener,
                started=not first,
                logger=self.logger,
            )

            audio = np.concatenate((audio, new_audio), 0)

            self._log_event("transcribing", "STT")
            text = self.transcribe(audio)
            self._log_event("transcribed", "STT", text)

            self._log_event("segmenting", "STT", text)
            first = False
            if len(seg.segment(text + " .")) > 1:
                sentence_finished = True
                self._log_event("sentence boundary detected", "STT", text)
            else:
                n -= 1
                self._log_event(
                    "no sentence boundary detected",
                    "STT",
                    text + ". tries left: " + str(n),
                )
        return text

    def _listen_stream(self) -> str:
        """
        records audio using record_user and returns its transcription
        :return: transcription
        """

        audio_queue = Queue()
        transcription_queue = Queue()

        audio_thread = Thread(
            target=record_user_stream,
            args=(self.silence_seconds, self.vad, audio_queue, self.listener),
        )
        transcription_thread = Thread(
            target=self.transcribe_stream, args=(audio_queue, transcription_queue)
        )

        audio_thread.start()
        transcription_thread.start()

        text = ""
        while True:
            _ = transcription_queue.get()
            if _ is None:
                break
            text += _ + " "
        audio_thread.join()
        transcription_thread.join()
        return text

    def listen(self) -> str:
        """
        records audio using record_user and returns its transcription
        :return: transcription
        """
        if self.stream:
            return self._listen_stream()
        else:
            return self._listen()

    def interrupt_listen(self, record_seconds=100) -> str:
        """
        Records audio with interruption. Transcribes audio if
        voice activity detected and returns True if transcription indicates
        interruption.

        :param record_seconds: Max seconds to record for
        :return: boolean indicating the if an interruption occured
        """
        if not self.listen_interruptions:
            return False
        while record_seconds > 0:
            interruption_audio = record_interruption(
                self.vad, record_seconds, streamer=self.listener, logger=self.logger
            )
            # duration of interruption audio
            if interruption_audio is None:
                return ""
            else:
                duration = len(interruption_audio) / 16_000
                self._log_event(
                    "transcribing interruption", "STT", f"{duration} seconds"
                )
                if self.stream:
                    text = self._sim_transcribe_stream(interruption_audio)
                else:
                    text = self.transcribe(interruption_audio)
                self._log_event("interruption transcribed", "STT", text)
                # remove any punctuation using re
                text = re.sub(r"[^\w\s]", "", text)
                text = text.lower()
                text = text.strip()
                if text in self.not_interrupt_words:
                    self._log_event("not interruption", "STT", text)
                    record_seconds -= duration
                else:
                    return text
