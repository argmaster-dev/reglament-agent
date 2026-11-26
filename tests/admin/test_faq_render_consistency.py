from unittest.mock import patch

from tests.admin.test_concurrent_faq_review import ConfirmForm
from tests.admin.test_versions import V, client, docx, env, pub, review_setup, status


def test_approval_uses_the_same_revision_as_the_page_text(env):
    faq_id = review_setup(env)
    browser = client('expert')
    current_history = V.history
    latest = docx('Регламент', '1. Раздел', '1.1. Срок составляет три дня.',
                  '1.2. Согласование выполняет секретарь комиссии.')

    def concurrent_publication(reader, *args, **kwargs):
        # The ordinary publisher uses its own connection while HTML for the expert is being assembled.
        pub(env, latest)
        return current_history(reader, *args, **kwargs)

    with patch.object(V, 'history', side_effect=concurrent_publication):
        page = browser.get(f'/admin/faq/{faq_id}/review')
    assert page.status_code == 200
    assert 'директор' in page.text and 'секретарь комиссии' not in page.text
    form = ConfirmForm(page.text)
    assert form.url
    response = browser.post(form.url, data=form.fields)
    assert status(env, faq_id) == 'review', 'A page displaying v2 qualified as approval for unseen v3'
    assert response.status_code == 409
