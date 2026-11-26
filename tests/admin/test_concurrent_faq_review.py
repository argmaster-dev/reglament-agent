from html.parser import HTMLParser

from tests.admin.test_versions import client, docx, env, pub, review_setup, status


class ConfirmForm(HTMLParser):
    def __init__(self, markup):
        super().__init__()
        self.url = None
        self.fields = {}
        self.in_form = False
        self.feed(markup)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'form':
            self.in_form = attrs.get('action', '').endswith('/confirm')
            if self.in_form:
                self.url = attrs['action']
        elif self.in_form and tag == 'input' and attrs.get('name'):
            self.fields[attrs['name']] = attrs.get('value', '')

    def handle_endtag(self, tag):
        if tag == 'form':
            self.in_form = False


def test_expert_confirms_only_the_text_they_actually_reviewed(env):
    faq_id = review_setup(env)
    browser = client('expert')
    path = f'/admin/faq/{faq_id}/review'
    response = browser.get(path)
    assert response.status_code == 200 and 'директор' in response.text
    old_form = ConfirmForm(response.text)
    assert old_form.url
    pub(env, docx('Регламент', '1. Раздел', '1.1. Срок составляет три дня.',
                  '1.2. Согласование выполняет секретарь комиссии.'))
    assert status(env, faq_id) == 'review'
    response = browser.post(old_form.url, data=old_form.fields)
    assert status(env, faq_id) == 'review', 'Old form approved a different revision of the source'
    assert response.status_code == 409
