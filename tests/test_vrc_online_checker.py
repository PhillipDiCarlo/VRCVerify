# tests/test_vrc_online_checker.py
# Pytest suite for src/vrc_online_checker.py
# To run: install pytest and run `pytest` from the project root

import logging
import pytest

# Enable pytest’s live logging of our INFO/debug messages
def pytest_configure(config):
    config.option.log_cli = True
    config.option.log_cli_level = logging.INFO

import sys
import os
import time
import threading
import json
import imaplib
import pika
import queue

# Ensure src/ is on the path
TEST_DIR = os.path.dirname(__file__)
SRC_DIR = os.path.abspath(os.path.join(TEST_DIR, os.pardir, 'src'))
sys.path.insert(0, SRC_DIR)

import vrc_online_checker as checker

# --- Dummy classes for mocking external dependencies ---
class DummyMail:
    def login(self, user, pwd): pass
    def select(self, mailbox): pass
    def search(self, charset, criterion):
        return ('OK', [b'1 2'])
    def fetch(self, email_id, parts):
        return ('OK', [(None, b'Subject: Your One-Time Code is 654321\r\n')])
    def logout(self): pass

class DummyUsersApi:
    def __init__(self, client): pass
    def get_user(self, vrc_user_id):
        class FakeUser:
            age_verification_status = '18+'
            bio = '654321'
        return FakeUser()

# --- Tests for core functionality ---

def test_fetch_latest_2fa_code(monkeypatch):
    monkeypatch.setattr(checker.time, 'sleep', lambda s: None)
    monkeypatch.setattr(imaplib, 'IMAP4_SSL', lambda host: DummyMail())
    code = checker.fetch_latest_2fa_code()
    assert code == '654321'

def test_login_to_vrchat_no_2fa(monkeypatch):
    dummy_user = type('User', (), {'display_name': 'TestUser'})()
    class DummyAuthApi:
        def __init__(self, client): pass
        def get_current_user(self): return dummy_user

    monkeypatch.setattr(checker.authentication_api, 'AuthenticationApi', DummyAuthApi)
    client = checker.login_to_vrchat()
    assert client is not None
    assert hasattr(client, 'user_agent')

def test_verify_and_build_result(monkeypatch):
    monkeypatch.setattr(checker, 'vrchat_api_client', object())
    monkeypatch.setattr(checker.users_api, 'UsersApi', DummyUsersApi)

    res1 = checker.verify_and_build_result('d1', 'u1', 'g1', 'wrong')
    assert res1['code_found'] is False
    assert res1['is_18_plus'] is True

    res2 = checker.verify_and_build_result('d2', 'u2', 'g2', '654321')
    assert res2['code_found'] is True
    assert res2['is_18_plus'] is True

def test_send_verification_result(monkeypatch):
    class DummyChannel:
        def __init__(self): self.published = []
        def queue_declare(self, queue, durable): pass
        def basic_publish(self, exchange, routing_key, body):
            self.published.append((exchange, routing_key, body))

    class DummyConn:
        def __init__(self): self.ch = DummyChannel()
        def channel(self): return self.ch
        def close(self): pass

    dummy_conn = DummyConn()
    monkeypatch.setattr(pika, 'BlockingConnection', lambda params: dummy_conn)

    payload = {'foo': 'bar'}
    checker.send_verification_result(payload)

    assert dummy_conn.ch.published
    _, _, body = dummy_conn.ch.published[0]
    assert json.loads(body) == payload

def test_rate_limited_scheduler(monkeypatch):
    calls = []
    def task(id_):
        logging.info(f"Running task {id_}")
        calls.append((id_, time.time()))

    sched = checker.RateLimitedScheduler(interval_seconds=0.02)
    sched.schedule(task, 1)
    sched.schedule(task, 2)
    sched.schedule(task, 3)

    # give it some time to run all three
    time.sleep(0.1)
    assert len(calls) == 3

    # ensure they ran in order
    ids = [i for i, _ in calls]
    assert ids == [1, 2, 3]

# -------------------------------------------------------------------
# Concurrency test with 23 real VRChat user IDs
# -------------------------------------------------------------------

VRC_USER_IDS = [
    "usr_75836826-c607-4f53-a8ac-08115e90701d",
    "usr_8f1670d6-f91c-40e4-b175-0530ecab60e0",
    "usr_4c57f059-da85-474f-bdda-4df7efbb3c17",
    "usr_a08f0340-2774-4ee0-9f1e-5c06d8404745",
    "usr_97a557c8-19b8-4399-8833-a17987901448",
    "usr_37751c7f-6bb6-462e-9568-2c61953eba9d",
    "usr_37751c7f-6bb6-462e-9568-2c61953eba9d",
    "usr_3ddb29dd-5090-436f-bd1f-a8f791c3d52e",
    "usr_5e9f1023-ab2c-4034-a94d-8f809447e18c",
    "usr_5cba7bcb-36a9-4993-a716-2263ec347f68",
    "usr_d3d5144b-5e40-410b-abcc-eb914e24134d",
    "usr_04f5b6be-6127-4c0b-9ba8-929e353f9c93",
    "usr_fb8de2a0-2d20-45f3-8ef6-2edbfd9d7bc1",
    "usr_e613bfc1-d212-42b6-84bb-55d3d56be619",
    "usr_4c002462-8b84-4c8f-a238-2ee5a0ffd053",
    "usr_bb33b9f7-0a51-44e5-875a-d9df1de6b2dd",
    "usr_7f1d6701-d13b-4646-bf1e-e7b854b8e232",
    "usr_650e279f-2c66-4bac-b768-7e56ea3db547",
    "usr_479a735f-ec50-4165-92a7-4fee022a5658",
    "usr_cbe89c75-c33c-436d-bf57-318c7ac5952a",
    "usr_0052ee3b-e91d-4bd3-a625-55b8edf6a36b",
    "usr_72fa9758-e74d-488d-a2fe-709ccc98068d",
    "usr_ae182702-866c-41e6-ac88-36d5865e6bc6"
]

def test_process_verification_request_concurrent(monkeypatch):
    # use a thread-safe queue to collect scheduled calls
    scheduled_q = queue.Queue()

    class DummySched:
        def schedule(self, func, data):
            logging.info(f"Scheduling {data['discordID']} -> {data['vrcUserID']}")
            scheduled_q.put((func, data))

    # swap in our dummy scheduler
    monkeypatch.setattr(checker, 'scheduler', DummySched())

    threads = []
    for idx, vrc_id in enumerate(VRC_USER_IDS):
        body = json.dumps({
            "discordID":        f"fake_discord_{idx:02d}",
            "vrcUserID":        vrc_id,
            "guildID":          "test_guild",
            "verificationCode": None
        })
        t = threading.Thread(
            target=checker.process_verification_request,
            args=(None, None, None, body)
        )
        threads.append(t)
        t.start()

    for t in threads:
        t.join()

    # drain the queue
    scheduled = []
    while not scheduled_q.empty():
        scheduled.append(scheduled_q.get())

    # all 23 requests should have been scheduled
    assert len(scheduled) == len(VRC_USER_IDS)

    # verify each payload
    for idx, (func, data) in enumerate(scheduled):
        assert func is checker.handle_verification
        assert data["discordID"] == f"fake_discord_{idx:02d}"
        assert data["vrcUserID"] == VRC_USER_IDS[idx]
        assert data["guildID"]   == "test_guild"
        assert data["verificationCode"] is None
