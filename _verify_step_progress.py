import asyncio, json, sys
sys.path.insert(0, '.')

import httpx
from googleapiclient.http import MediaFileUpload

import config
import database
from main import app
from routers.auth import get_current_user
from services import drive

TEST_FILES = [
    (r"C:\Users\Win11-A\Desktop\bank_statements\length_wise\AU_Bank\1_singlepage.pdf", "verify_step_1.pdf"),
    (r"C:\Users\Win11-A\Desktop\bank_statements\length_wise\AU_Bank\2_twopage.pdf", "verify_step_2.pdf"),
]

async def main():
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 1, "username": "verify_user", "role": "company_admin",
        "level": 0, "schema": "company_028", "company_id": 28,
    }
    await database.init_pool()

    service = drive._get_service()
    uploaded_ids = []
    for path, name in TEST_FILES:
        media = MediaFileUpload(path, mimetype="application/pdf")
        f = service.files().create(
            body={"name": name, "parents": [config.DRIVE_FOLDER_ID]},
            media_body=media, fields="id, name", supportsAllDrives=True,
        ).execute()
        uploaded_ids.append(f["id"])
        print("uploaded:", f["name"])

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=60) as client:
        r = await client.post("/imports/from-drive", data={})
        job_id = r.json()["job_id"]
        print("started:", r.json())

        snapshots = []
        job = None
        for i in range(60):
            jr = await client.get(f"/imports/jobs/{job_id}")
            job = jr.json()
            snap = {k: job[k] for k in
                    ("state", "batch_index", "batch_total", "batch_label",
                     "percent", "message", "batches_done")}
            if not snapshots or snapshots[-1] != snap:
                snapshots.append(snap)
                print(json.dumps(snap))
            if job["state"] in ("done", "failed"):
                break
            await asyncio.sleep(0.3)

        print("\nFINAL RESULT:", json.dumps(job["result"], indent=2))

        # Clean up: discard any batches this created, delete test files from Drive.
        async with database.company_connection("company_028") as conn:
            for f in job["result"]["files"]:
                b = await conn.fetchrow(
                    "SELECT id FROM import_batches WHERE filename = $1 ORDER BY id DESC LIMIT 1",
                    f["name"].strip(),
                )
                if b:
                    dr = await client.delete(f"/imports/batches/{b['id']}")
                    print("discarded batch for", f["name"], "->", dr.status_code)

    for fid in uploaded_ids:
        service.files().delete(fileId=fid, supportsAllDrives=True).execute()
    remaining = drive.list_folder_files(config.DRIVE_FOLDER_ID)
    for f in remaining:
        if f["name"].startswith("verify_step"):
            service.files().delete(fileId=f["id"], supportsAllDrives=True).execute()
    print("cleanup done")

    await database.close_pool()

asyncio.run(main())
