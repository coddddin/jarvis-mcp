# 데이터 암호화 백업 (restic → Cloudflare R2)

코드는 GitHub `coddddin/jarvis-mcp`에 있다. Git에 올리지 않는 `JarvisWorkspace/`와 `.jarvis-auth/`는 이 절차로 백업한다. restic이 이 PC에서 먼저 암호화한 뒤 R2로 보낸다.

## 구성

- `scripts/backup/stage_data.py`: 두 폴더를 프로젝트 밖 임시 폴더로 모은다. SQLite는 온라인 backup API로 복사하고 `integrity_check`를 통과해야 한다. 링크는 따라가지 않는다.
- `scripts/backup/backup.ps1`: 모으기 → `restic backup` → 보존 정책(일 7·주 4·월 6) 정리 → 임시 폴더 삭제.
- `scripts/backup/restore-check.ps1`: 최신 스냅샷을 빈 임시 폴더로 복원하고 `restic check`와 DB 무결성을 확인한다. 실제 데이터는 건드리지 않는다.
- `scripts/backup/register-task.ps1`: 매일 23:00 작업 스케줄러 등록. PC가 꺼져 있었으면 다음 기회에 실행한다.
- 비밀값: `%LOCALAPPDATA%\JarvisBackup\secrets.clixml`에 Windows DPAPI로 암호화해 저장한다. 현재 Windows 사용자만 풀 수 있다. 로그는 같은 폴더의 `backup.log`.

## 처음 설정

1. Cloudflare에서 R2를 활성화하고 버킷(예: `jarvis-backup`)을 만든다.
2. R2 API 토큰을 **해당 버킷만, Object Read & Write** 권한으로 만든다. Access Key ID, Secret Access Key, Account ID를 확인한다.
3. 직접 연 PowerShell 창에서 실행한다. AI 세션에 키를 붙여넣지 않는다.
   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts\backup\setup-secrets.ps1
   ```
   표시되는 **백업 암호를 비밀번호 관리자에 따로 보관**한다. PC가 고장 나면 이 암호와 R2 키 없이는 복원할 수 없다.
4. 첫 백업과 복원 확인, 예약 등록:
   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts\backup\backup.ps1
   powershell -ExecutionPolicy Bypass -File scripts\backup\restore-check.ps1
   powershell -ExecutionPolicy Bypass -File scripts\backup\register-task.ps1
   ```

## 다른 PC에서 복원

restic을 설치하고 `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_DEFAULT_REGION=auto`, `RESTIC_PASSWORD`, `RESTIC_REPOSITORY=s3:https://<Account ID>.r2.cloudflarestorage.com/<버킷>`을 설정한 뒤 `restic restore latest --tag jarvis --target <폴더>`를 실행한다. 복원본의 두 폴더를 프로젝트 루트에 둔다.

## 한계

- 이 PC의 백업 시점까지만 보존된다. 실행 중 DB는 backup API로 일관성 있게 복사하지만 서버 동작 전체를 멈춘 시점 복사는 아니다.
- `.integrations/`(외부 도구, 약 1.6GB)와 `artifacts/`는 백업하지 않는다. 다시 설치·생성할 수 있다.
- 2026-10-21 실제 R2 첫 백업과 복원 확인 성공: 29개 파일, SQLite 3개 무결성 ok. 매일 23:00 예약 등록. 복원 확인은 복호화된 사본을 검증 직후 삭제한다.
- `restore-check.ps1`의 `restic check`는 저장소 구조만 확인한다. 전체 데이터 재검증이 필요하면 `restic check --read-data`를 따로 실행한다.
