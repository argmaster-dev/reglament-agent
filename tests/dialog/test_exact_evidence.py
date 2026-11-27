from reglament_agent.answer import Outcome, SourceRef
from reglament_agent.dialog.grounding import Anchor, assemble, model_ref


def evidence(question, sentences):
    anchors = {str(i): Anchor(SourceRef('guide', 'Правила', 'Снабжение', 2, str(i+1), 0), text, '', i)
               for i, text in enumerate(sentences)}
    claims = [{'ref': model_ref(key, anchor), 'text': anchor.text} for key,anchor in anchors.items()]
    return assemble(question, {'outcome':'answer', 'claims':claims}, anchors, [], False, {})


def test_statement_preserves_source_wording_including_note_prefix():
    text = 'Важно: Процесс закупки должен быть инициирован заранее.'
    result = evidence('Когда должен быть инициирован процесс закупки?', [text])
    assert result.statements[0].text == text


def test_mention_of_requester_does_not_hide_general_rule():
    result = evidence('SS спрашивает: меняется ли приоритет заявки?', [
        'Приоритет заявки не меняется.', 'Диспетчер может повысить приоритет заявки при аварии.'])
    assert result.outcome == Outcome.CONFLICT and len(result.statements)==2


def test_short_legal_entity_name_matches_only_the_same_party_in_a_signed_agreement():
    question = 'С какого юрлица заключаем субдоговоры?'
    correct = 'Субподрядные договоры заключаются от юридических лиц компании, кроме перепродажи сервисов.'
    legal_review = 'Соглашения заключаются после рассмотрения юридической службой.'
    individual = 'Соглашения заключаются с физическими лицами.'
    result = evidence(question, [legal_review, individual, correct])
    assert [s.text for s in result.statements] == [correct]


def test_full_legal_entity_name_matches_an_abbreviated_rule():
    rule = 'Поручение оформляет представитель юр. лица.'
    result = evidence('От какого юридического лица оформляется заявка?', [rule])
    assert [s.text for s in result.statements] == [rule]
