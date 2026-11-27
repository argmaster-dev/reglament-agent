from uuid import uuid4

from psycopg.types.json import Jsonb

from reglament_agent.answer import Answer, Outcome
from reglament_agent.chat.history import load, render
from reglament_agent.dialog.state import answers


def test_employee_histories_after_returning_to_chat(isolated):
    conn, _ = isolated
    first = "employee-alpha"
    second = "employee-beta"
    dialogue = "session-01"
    entries = [
        (first, "Срок пресейла", "Три дня"),
        (second, "Ответственный за заявку", "Логист"),
        (first, "Основание срока", "Пункт 1.5"),
    ]
    for employee, question, answer in entries:
        payload = answers.dump_python(Answer(Outcome.ANSWER, summary=answer), mode="json")
        conn.execute(
            """INSERT INTO dialog_requests(id,user_email,dialog_id,question,restated,answer,trace)
               VALUES(%s,%s,%s,%s,'',%s,'{}')""",
            (uuid4(), employee, dialogue, question, Jsonb(payload)),
        )

    for employee in (first, second):
        own = [(question, answer) for owner, question, answer in entries if owner == employee]
        index = load(conn, employee)
        assert [(item[0], item[1]) for item in index] == [(dialogue, own[0][0])]
        detail = load(conn, employee, index[0][0])
        assert [row[0] for row in detail] == [question for question, _ in own]
        response = render(detail, dialogue)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        html = response.body.decode()
        assert html.count("<article>") == len(own)
        for question, answer in own:
            assert question in html and answer in html
