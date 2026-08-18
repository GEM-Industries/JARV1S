import logging
import numpy as np
from ten_vad import TenVad

logger = logging.getLogger(__name__)

class TenVADService:
    """
    Voice Activity Detection service using TEN VAD.
    Standardized on 16kHz audio.
    """
    
    def __init__(self, hop_size: int = 256, threshold: float = 0.5):
        self.hop_size = hop_size
        self.threshold = threshold
        try:
            self.vad = TenVad(hop_size=hop_size, threshold=threshold)
            logger.info(f"TEN VAD initialized with hop_size={hop_size}, threshold={threshold}")
        except Exception as e:
            logger.error(f"Failed to initialize TEN VAD: {e}")
            raise

    def is_speech(self, audio_bytes: bytes) -> bool:
        """
        Check for speech in the given audio bytes.
        Expects raw PCM 16-bit audio at 16kHz.
        """
        if not audio_bytes:
            return False

        try:
            # Convert bytes to int16 numpy array
            audio_np = np.frombuffer(audio_bytes, dtype=np.int16)
            
            # TEN VAD requires chunks of exactly hop_size
            # We process the audio in hop_size increments
            num_samples = len(audio_np)
            for i in range(0, num_samples, self.hop_size):
                chunk = audio_np[i:i + self.hop_size]
                
                # If the last chunk is smaller than hop_size, pad with zeros
                if len(chunk) < self.hop_size:
                    chunk = np.pad(chunk, (0, self.hop_size - len(chunk)))
                
                # process returns (probability, flag) where flag 1 is speech
                _, flag = self.vad.process(chunk)
                if flag == 1:
                    return True
            
            return False
        except Exception as e:
            logger.error(f"Error in TEN VAD processing: {e}")
            return False
