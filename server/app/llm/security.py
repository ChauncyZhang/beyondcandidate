from cryptography.fernet import Fernet,InvalidToken

class ApiKeyCipher:
    def __init__(self,key:bytes):
        try: self._cipher=Fernet(key)
        except Exception: raise ValueError("invalid LLM encryption key") from None
    def encrypt(self,value:str)->bytes:
        if not value or len(value)>4096: raise ValueError("invalid API key")
        return self._cipher.encrypt(value.encode())
    def decrypt(self,value:bytes)->str:
        try: return self._cipher.decrypt(value).decode()
        except (InvalidToken,UnicodeDecodeError): raise ValueError("LLM API key cannot be decrypted") from None
