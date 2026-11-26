import os

from reglament_agent.answer import PointView
from reglament_agent.chat.services import Services
from reglament_agent.storage.sources import point_view

from .route import Dialog


def source_view(conn, ref):
    if ref.faq_id is None:
        return point_view(conn, ref)
    row = conn.execute(
        """SELECT f.question,f.answer,f.updated_at,d.department,v.version_number
                       FROM faq_entries f LEFT JOIN documents d ON f.doc_id=d.doc_id
                       LEFT JOIN document_versions v ON f.version_id=v.version_id
                       WHERE f.id=%s AND f.status='active' AND (f.basis_kind='outside'
                       OR (d.status='active' AND d.active_version_id=f.version_id))
                       AND coalesce(v.version_number,0)=%s""",
        (ref.faq_id, ref.version_no),
    ).fetchone()
    if not row:
        raise LookupError("Ответ эксперта снят с публикации или обновлён.")
    return PointView(
        "FAQ службы качества",
        row[3] or "Служба качества",
        [],
        row[4] or 0,
        row[2].isoformat(),
        [row[0]],
        "",
        [row[1]],
        0,
        None,
        [],
    )


class Sources:
    def __init__(self, profiles):
        self.profiles = profiles

    async def point(self, ref):
        return await self.profiles.run(source_view, ref)


def for_database(dsn):
    dialog = Dialog(dsn)
    return Services(dialog, Sources(dialog.profiles), dialog.profiles)


def build_services():
    return for_database(os.environ["DATABASE_URL"])
