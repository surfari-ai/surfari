import difflib
import keyring
import os
import stat
import platform
from cryptography.fernet import Fernet
import surfari.util.db_service as db_service
import surfari.util.config as config

class SiteCredentialManager:
    def __init__(self, db_path=None, service_name="SurfariEncryptionKey", keyring_user="encryption"):
        self.service_name = service_name
        self.keyring_user = keyring_user
        self.fernet = Fernet(self._get_or_create_key())

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        pass  # Nothing to clean up directly

    def _get_or_create_key(self):
        system = platform.system()
        use_system_keyring = config.CONFIG["app"].get("use_system_keyring", False)
        if system == "Linux" or (system == "Darwin" and not use_system_keyring):
            # Linux: use file-based key in ~/.surfari/key_string
            key_file_path = os.path.join(os.path.expanduser("~"), ".surfari", "key_string")
            key_dir = os.path.dirname(key_file_path)
            os.makedirs(key_dir, exist_ok=True)

            if not os.path.exists(key_file_path):
                key = Fernet.generate_key()
                with open(key_file_path, "wb") as f:
                    f.write(key)
                # Restrict permissions: rw------- (chmod 600)
                os.chmod(key_file_path, stat.S_IRUSR | stat.S_IWUSR)
            else:
                with open(key_file_path, "rb") as f:
                    key = f.read()

            return key

        # macOS / Windows: use keyring
        key = keyring.get_password(self.service_name, self.keyring_user)
        if key is None:
            key = Fernet.generate_key().decode()
            keyring.set_password(self.service_name, self.keyring_user, key)
        return key.encode()


    def encrypt(self, value: str) -> bytes:
        return self.fernet.encrypt(value.encode()) if value else b""

    def decrypt(self, encrypted: bytes) -> str:
        return self.fernet.decrypt(encrypted).decode() if encrypted else ""

    def save_credentials(self, site_name: str, url: str, username: str, password: str):
        encrypted_username = self.encrypt(username)
        encrypted_password = self.encrypt(password)
        with db_service.get_db_connection_sync() as conn:
            conn.execute("""
                INSERT INTO credentials (site_name, url, encrypted_username, encrypted_password)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(site_name) DO UPDATE SET
                    url = excluded.url,
                    encrypted_username = excluded.encrypted_username,
                    encrypted_password = excluded.encrypted_password
            """, (site_name, url, encrypted_username, encrypted_password))
            conn.commit()


    def get_credentials(self, site_name: str):
        with db_service.get_db_connection_sync() as conn:
            cursor = conn.execute("""
                SELECT url, encrypted_username, encrypted_password
                FROM credentials
                WHERE site_name = ?
            """, (site_name,))
            row = cursor.fetchone()
        if not row:
            return None
        url, enc_user, enc_pass = row
        return {
            "site_name": site_name,
            "url": url,
            "username": self.decrypt(enc_user),
            "password": self.decrypt(enc_pass)
        }

    def list_sites(self):
        with db_service.get_db_connection_sync() as conn:
            cursor = conn.execute("SELECT DISTINCT site_name FROM credentials")
            return [row[0] for row in cursor.fetchall()]

    def list_all_credentials(self, show_decrypted=False):
        with db_service.get_db_connection_sync() as conn:
            cursor = conn.execute("""
                SELECT site_id, site_name, url, encrypted_username, encrypted_password
                FROM credentials
                ORDER BY site_id
            """)
            rows = cursor.fetchall()

        if not rows:
            print("No credentials stored.")
            return

        for row in rows:
            id_, site_name, url, enc_user, enc_pass = row
            print(f"[{id_}] Site: {site_name}")
            print(f"     URL: {url}")
            if show_decrypted:
                try:
                    username = self.decrypt(enc_user)
                    password = self.decrypt(enc_pass)
                    print(f"     Username: {username}")
                    print(f"     Password: {password}")
                except Exception as e:
                    print(f"     🔐 Decryption failed: {e}")
            print()




    def load_site_with_secrets(self, site_id: int) -> dict:
        """
        Returns a dictionary of all decrypted credentials keyed by site_id.
        """
        secret_dict = {}
        with db_service.get_db_connection_sync() as conn:
            cursor = conn.execute("""
                SELECT site_id, encrypted_username, encrypted_password, site_name, url
                FROM credentials
                WHERE site_id = ?
            """, (site_id,))
            rows = cursor.fetchall()

        for row in rows:
            site_id, enc_user, enc_pass, site_name, url = row
            try:
                username = self.decrypt(enc_user)
                password = self.decrypt(enc_pass)
                secret_dict = {
                    "UsernameAssistant": username,
                    "PasswordAssistant": password,
                    "SiteName": site_name,
                    "URL": url
                }
            except Exception as e:
                print(f"Failed to decrypt credentials for site '{site_id}': {e}")
        return secret_dict

    def delete_site(self, site_name: str):
        with db_service.get_db_connection_sync() as conn:
            conn.execute("DELETE FROM credentials WHERE site_name = ?", (site_name,))
            conn.commit()
            
    def find_site_info_by_name(self, query_name: str, cutoff: float = 0.9):
        """
        Perform case-insensitive fuzzy search to find site_id and url based on site_name.
        Returns the best match (site_id, site_name, url), or None if no match found above cutoff.
        """
        with db_service.get_db_connection_sync() as conn:
            cursor = conn.execute("""
                SELECT site_id, site_name, url
                FROM credentials
            """)
            records = cursor.fetchall()

        if not records:
            return None

        site_names = [row[1] for row in records]
        matches = difflib.get_close_matches(query_name.lower(), [s.lower() for s in site_names], n=1, cutoff=cutoff)

        if not matches:
            return None

        best_match_lower = matches[0]
        for site_id, site_name, url in records:
            if site_name.lower() == best_match_lower:
                return {
                    "site_id": site_id,
                    "site_name": site_name,
                    "url": url
                }

        return None  # Should not reach here unless a logic bug occurs
