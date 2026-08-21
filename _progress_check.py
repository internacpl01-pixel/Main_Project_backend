"""Temporary: watch the per-batch progress readings on a real batched parse.

Dry run (save=False) so nothing is written to any company's staging.
"""
import asyncio
import sys
import time

sys.path.insert(0, ".")

from services import jobs
from services.pdf_import import start_pdf_job

PDF = r"C:\Users\Win11-A\Desktop\AMB KVB 65 Pages 17860443.pdf"
SCHEMA = "company_028"


async def main():
    data = open(PDF, "rb").read()
    started = await start_pdf_job(
        schema=SCHEMA, file_bytes=data, filename="progress-check.pdf",
        username="amb-admin", bank_id=None, password="", save=False,
        pages_spec="1-12", batch_pages=4,
    )
    print("start:", started)
    job_id = started["job_id"]

    seen = None
    t0 = time.time()
    while True:
        j = jobs.get(job_id)
        key = (j["state"], j["batch_index"], j["percent"], len(j["batches_done"]))
        if key != seen:
            seen = key
            print(f"{time.time() - t0:6.1f}s  {j['state']:8s} "
                  f"batch {j['batch_index']}/{j['batch_total']}  "
                  f"bar {j['percent']:3d}%  file {j['overall_percent']:3d}%  "
                  f"| {j['message']}")
            for b in j["batches_done"]:
                print(f"           done: batch {b['index']} of {b['total']} "
                      f"· {b['label']} · {b['rows']} rows")
        if j["state"] in ("done", "failed"):
            print("\nerror:", j["error"])
            if j["result"]:
                r = j["result"]
                print("rows:", r["row_count"], "batches:", r["batches"],
                      "pages_parsed:", r["pages_parsed"])
            return
        await asyncio.sleep(0.25)


asyncio.run(main())
