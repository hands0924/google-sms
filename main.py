import os
import json
import logging
from flask import Flask, request, jsonify
from google.cloud import firestore, tasks_v2
from google.api_core.exceptions import AlreadyExists
from solapi import SolapiMessageService
from solapi.model import RequestMessage

# ─── Flask & logging ────────────────────────────────────────────────────────
app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ─── Env vars ────────────────────────────────────────────────────────────────
PROJECT_ID     = os.getenv('GCP_PROJECT', 'prism-fin')
QUEUE_LOC      = os.getenv('QUEUE_LOCATION', 'us-central1')
QUEUE_ID       = os.getenv('QUEUE_ID', 'sms-queue')
SEND_SMS_URL   = os.getenv('SEND_SMS_URL')   # set AFTER deploy
SOLAPI_KEY     = os.getenv('SOLAPI_API_KEY')
SOLAPI_SECRET  = os.getenv('SOLAPI_API_SECRET')
SOLAPI_SENDER  = os.getenv('SOLAPI_SENDER')

# ─── Lazy clients ───────────────────────────────────────────────────────────
_fs, _tasks = None, None
def get_firestore():
    global _fs
    if _fs is None:
        _fs = firestore.Client(project=PROJECT_ID)
    return _fs

def get_tasks_client():
    global _tasks
    if _tasks is None:
        _tasks = tasks_v2.CloudTasksClient()
    return _tasks

# ─── ➊ Enqueue helper ────────────────────────────────────────────────────────
def enqueue_sms_task(payload: dict):
    client = get_tasks_client()
    parent = client.queue_path(PROJECT_ID, QUEUE_LOC, QUEUE_ID)
    task = {
        "http_request": {
            "http_method": tasks_v2.HttpMethod.POST,
            "url": SEND_SMS_URL,
            "headers": {"Content-Type":"application/json"},
            "body": json.dumps(payload).encode()
        }
    }
    resp = client.create_task(request={"parent": parent, "task": task})
    logger.info(f"Enqueued Cloud Task: {resp.name}")

# ─── ➋ SMS send helper ───────────────────────────────────────────────────────
def send_sms(phone: str, name: str, inquiry: str) -> bool:
    svc = SolapiMessageService(SOLAPI_KEY, SOLAPI_SECRET)
    text = (
        f"[포용적 금융서비스, 프리즘지점]\n"
        f"{name}님, {inquiry} 문의 감사합니다. 곧 연락드리겠습니다.\n\n"
        "프리즘지점 드림"
    )
    msg = RequestMessage(from_=SOLAPI_SENDER, to=phone, text=text)
    resp = svc.send(msg)
    failed = resp.group_info.count.registered_failed
    if failed > 0:
        raise RuntimeError(f"{failed} messages failed")
    return True

# ─── 1) Webhook ─────────────────────────────────────────────────────────────
@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.get_json(force=True)
    for k in ("timestamp","phone","name","inquiry"):
        if k not in data:
            return jsonify(error=f"Missing '{k}'"), 400

    db = get_firestore()
    # dedupe key: timestamp+phone
    doc_id = f"{data['timestamp']}_{data['phone']}"\
             .replace(" ", "_").replace(":", "-")
    doc_ref = db.collection('submissions').document(doc_id)
    try:
        doc_ref.create({**data, "receivedAt": firestore.SERVER_TIMESTAMP})
    except AlreadyExists:
        logger.info("Duplicate submission detected; skipping enqueue")
        return ('', 200)

    enqueue_sms_task(data)
    return ('', 202)

# ─── 2) SMS handler (Cloud Tasks) ────────────────────────────────────────────
@app.route('/send-sms', methods=['POST'])
def send_sms_handler():
    payload = request.get_json(force=True)
    try:
        send_sms(payload["phone"], payload["name"], payload["inquiry"])
        return ('', 204)
    except Exception as e:
        logger.error(f"SMS send error: {e}")
        return ('', 500)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080)
