from pathlib import Path
from ai_runtime.client import Client

client = Client(Path('.runtime/token').read_text().strip())
session = client.create_session(Path.cwd())
sid = session['id']
print('Session:', sid)
print(client.wait(client.execute(sid, 'answer = 40')['id']))
# A completely new API client still addresses the same live kernel.
client = Client(Path('.runtime/token').read_text().strip())
print(client.wait(client.execute(sid, 'answer += 2\nprint(answer)')['id']))
print(client.wait(client.execute(sid,
    "runtime.checkpoint_save({'step': 'done', 'answer': answer})\n"
    "runtime.save_artifact('Answer: ' + str(answer), 'result.txt', 'text/plain')")['id']))
for event in client.events(sid)['events']:
    print(event['seq'], event['kind'], event['payload'])
# Leave the session alive so the next request can continue using `answer`.
