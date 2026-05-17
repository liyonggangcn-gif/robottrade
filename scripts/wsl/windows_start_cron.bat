@echo off
timeout /t 5 /nobreak >nul
wsl -u root -e sh -c "service cron start 2>/dev/null || service crond start 2>/dev/null || true"
