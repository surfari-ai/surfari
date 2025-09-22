
import socket
import json

async def send_to_electron(cmd):
    s = socket.create_connection(("127.0.0.1", 32123))
    s.sendall((json.dumps(cmd) + "\n").encode("utf-8"))
    data = s.recv(65536).decode("utf-8")
    s.close()
    return json.loads(data.strip())
