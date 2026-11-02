# Starting Áron by itself before each service

Áron can start on a schedule, so the translation is running before the first word
of the service even if nobody is at the computer yet.

- `church-translate.exe --autostart` opens the operator page as `/?autostart=1`:
  the page turns **Web** captions on (when a relay is configured) and starts
  listening, as if the operator had pressed **Start**. The parameter is taken off
  the address, so reloading the page doesn't start it again.
- `scripts/aron-schedule.ps1` sets up a Windows scheduled task that runs it before
  each service (Sunday 9:10 and 17:40, Tuesday 18:40 by default).

## Set it up (once, on the streaming computer)

1. Sign in as the operator, and start Áron from its page by hand once, choosing
   **Always allow** when the browser asks for the microphone. The scheduled start
   relies on that permission; it opens the same browser and profile.
2. Run, in PowerShell:

   ```powershell
   powershell -ExecutionPolicy Bypass -File aron-schedule.ps1
   ```

   It looks for `church-translate*.exe` under `Documents\aron-public`; pass
   `-AronDir <folder>` if it lives elsewhere, and `-Times 'Sunday 09:10','Tuesday 18:40'`
   for other times. No administrator rights needed.
3. Try it: `Start-ScheduledTask -TaskName 'Aron - start before services'`.

## How it behaves

- If Áron is already running (started by hand), the task does nothing, so it
  never listens twice.
- It always runs the newest `church-translate*.exe` in that folder, so updating
  Áron needs no new task.
- It wakes the computer from sleep for the start, and runs late if the computer
  was off at the time.
- It runs only while the operator is signed in (it needs the desktop for the page).
- Stopping and **Clear** after the service stay with the operator.

To remove it: `powershell -ExecutionPolicy Bypass -File aron-schedule.ps1 -Remove`.
