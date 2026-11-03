"""B2 파일 내려받기/올리기 (Actions 보조 작업용). 사용: python3 b2_sync.py get|put <B2 파일명> <로컬 경로>
get에서 파일이 없으면 종료 코드 3."""
import os
import sys
from b2sdk.v2 import InMemoryAccountInfo, B2Api

mode, name, path = sys.argv[1:4]
api = B2Api(InMemoryAccountInfo())
api.authorize_account("production", os.environ["B2_KEY_ID"], os.environ["B2_APPLICATION_KEY"])
bucket = api.get_bucket_by_name(os.environ.get("B2_BUCKET", "yt-daily-jh"))
if mode == "get":
    names = {fi.file_name for fi, _ in bucket.ls()}
    if name not in names:
        print(f"{name} not in bucket")
        sys.exit(3)
    bucket.download_file_by_name(name).save_to(path)
    print(f"downloaded {name} -> {path} ({os.path.getsize(path)/1e6:.1f} MB)")
elif mode == "put":
    bucket.upload_local_file(local_file=path, file_name=name)
    print(f"uploaded {path} -> {name} ({os.path.getsize(path)/1e6:.1f} MB)")
