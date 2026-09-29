"""İçerik silme (KVKK): metin, URL, metadata, istemci IP'si ve R2'deki kanıt kareleri silinir;
anonim karar kaydı istatistik için kalır. Silme API'si ve saklama süresi temizliği bunu kullanır."""
from app.storage import storage


async def purge_requests(conn, ids: list, *, forget_user: bool = False) -> list[str]:
    """Aynı transaction içinde çağrılmalı. Silinecek R2 anahtarlarını döner (commit'ten sonra silinmeli)."""
    if not ids:
        return []
    await conn.execute(
        f"""
        UPDATE moderation_requests
        SET content_text = NULL, content_url = NULL, metadata = '{{}}'::jsonb, client_ip = NULL, user_info_enc = NULL,
            content_purged_at = coalesce(content_purged_at, now())
            {", external_user_id = NULL, external_content_id = NULL" if forget_user else ""}
        WHERE id = ANY($1::uuid[])
        """,
        ids,
    )
    # İçeriği silinen öğe incelemede bekliyorsa kuyruktan çıkar (moderatör boş içerik görmesin)
    await conn.execute(
        "UPDATE review_queue SET status = 'resolved', reviewed_at = now() WHERE request_id = ANY($1::uuid[]) AND status = 'pending'",
        ids,
    )
    return [r["path"] for r in await conn.fetch(
        "DELETE FROM media_evidence WHERE request_id = ANY($1::uuid[]) RETURNING path", ids
    )]


async def remove_files(keys: list[str]) -> None:
    """R2'deki kanıt karelerini siler (commit'ten sonra çağrılır)."""
    if keys and storage.client is not None:
        await storage.delete_keys(keys)
