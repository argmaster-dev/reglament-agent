from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, BrokenBarrierError
from unittest.mock import patch

from tests.admin.test_lock_order import A, B, text
from tests.admin.test_versions import V, client, env, faq, pub, status


def test_two_experts_move_different_faqs_between_the_same_documents(env):
    pub(env, text('три'), A)
    pub(env, text('три'), B)
    fa = faq(env, 'Первый отдельный вопрос?', points=[('doc-a', '1.1')])
    fb = faq(env, 'Второй отдельный вопрос?', points=[('doc-b', '1.1')])
    pub(env, text('пять'), A)
    pub(env, text('пять'), B)
    assert status(env, fa) == status(env, fb) == 'review'
    forms = [(fid, target, V.shown_token(env, V.F.get_faq(env, fid)))
             for fid, target in [(fa, 'doc-b'), (fb, 'doc-a')]]
    gate = Barrier(2)
    original = V.F._capture_basis

    def together(*args, **kwargs):
        # If the operation already serializes both documents, the first request may proceed alone.
        try:
            gate.wait(timeout=1.5)
        except BrokenBarrierError:
            pass
        return original(*args, **kwargs)

    def send(form):
        fid, target, seen = form
        with client('expert') as browser:
            return browser.post(f'/admin/faq/{fid}/review/repin',
                                data={**seen, 'doc_id': target, 'number': '1.1'}).status_code

    with patch.object(V.F, '_capture_basis', side_effect=together), ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(send, forms))
    assert statuses == [303, 303]
    assert V.F.get_faq(env, fa)['doc_id'] == 'doc-b'
    assert V.F.get_faq(env, fb)['doc_id'] == 'doc-a'
