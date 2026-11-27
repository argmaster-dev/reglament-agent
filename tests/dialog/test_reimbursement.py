import asyncio

import pytest

from reglament_agent.answer import Outcome, UserContext
from reglament_agent.dialog.route import Dialog
from reglament_agent.storage.retrieve import Candidates
from tests.dialog.test_route import Model, hit, status


@pytest.mark.parametrize(('question', 'sentence', 'expected'), [
    ('По какому внутреннему порядку сотруднику возмещают покупку проездного билета?',
     'Работнику компенсируют расходы на проездной билет после представления квитанции.', Outcome.ANSWER),
    ('По какому внутреннему порядку сотруднику возмещают покупку проездного билета?',
     'Сотруднику передают проездной билет после регистрации покупки.', Outcome.NO_BASIS),
    ('Как работнику компенсируют аренду оборудования?',
     'Работник согласовывает заявку, а арендодателю возмещают стоимость аренды оборудования.', Outcome.NO_BASIS),
    ('Как сотруднику возмещают аренду оборудования?',
     'Сотруднику возмещают аренду помещения, а оборудование предоставляется безвозмездно.', Outcome.NO_BASIS),
    ('Как сотруднику возмещают аренду оборудования?',
     'Сотрудник возмещает поставщику расходы на аренду оборудования.', Outcome.NO_BASIS),
    ('Как сотруднику возмещают аренду оборудования?',
     'Вопрос о возможности компенсации сотруднику аренды оборудования передан на обсуждение.', Outcome.NO_BASIS),
    ('Как сотруднику возмещают аренду оборудования?',
     'Возмещение сотруднику расходов на аренду оборудования производится после утверждения сметы.', Outcome.ANSWER),
    ('Как сотруднику компенсируют зарядку?',
     'Работнику возмещают зарядку после представления квитанции.', Outcome.ANSWER),
    ('Как сотруднику возмещают аренду оборудования?',
     'Расходы работника на аренду оборудования возмещаются после утверждения заявки.', Outcome.ANSWER),
    ('Как сотруднику возмещают аренду оборудования?',
     'Для возмещения сотруднику расходов на аренду оборудования требуется приложить квитанцию.', Outcome.ANSWER),
    ('Как сотруднику возмещают аренду оборудования?',
     'Куратор уточняет, возмещают ли сотруднику аренду оборудования.', Outcome.NO_BASIS),
    ('Как компенсировать задержку проекта?',
     'Задержку проекта компенсируют за счёт резерва графика.', Outcome.ANSWER),
    ('Как заказчику возмещают аренду стенда?',
     'По договору заказчику компенсируют аренду стенда по подписанному акту.', Outcome.ANSWER),
    ('Как заказчику возмещают аренду стенда?',
     'По договору заказчик компенсирует поставщику аренду стенда по подписанному акту.', Outcome.NO_BASIS),
    ('Как сотруднику возмещают проездной билет?',
     'Расходы на проездной билет сотруднику не компенсируются.', Outcome.ANSWER),
])
def test_regulatory_reimbursement_opens_one_ticket_only_without_applicable_basis(isolated, question, sentence, expected):
    conn, dsn = isolated
    source = hit('Отдел', sentence)
    model = Model()
    model.result = {
        'outcome': 'answer', 'claims': [{
            'text': sentence, 'ref': {'id': 'S1', 'doc_id': source.doc_id, 'point': source.number, 'sentence': 0},
        }],
    }
    dialog = Dialog(dsn, model, search=lambda *args, **kw: Candidates([source], []), shortlist=lambda *args, **kw: [])
    user = UserContext('reviewer@example.org', 'employee', 'reimbursement', '1')
    reply = asyncio.run(dialog.answer(user, question, status))
    assert reply.outcome == expected
    # Re-delivery of one user turn must not add a ticket or a second outbox row.
    assert asyncio.run(dialog.answer(user, question, status)) == reply
    assert len(model.calls) == 1
    counts = conn.execute('SELECT (SELECT count(*) FROM tickets), (SELECT count(*) FROM mail_outbox)').fetchone()
    if expected == Outcome.NO_BASIS:
        assert not reply.statements and reply.ticket and counts == (1, 1)
        assert conn.execute('SELECT t.number,o.kind FROM tickets t JOIN mail_outbox o ON o.ticket_id=t.id').fetchone() == (
            reply.ticket.number, 'ticket_created',
        )
    else:
        assert [s.text for s in reply.statements] == [sentence] and not reply.ticket and counts == (0, 0)
