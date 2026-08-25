' Starts the RandomGenerals supervisor with no visible window.
'
' The supervisor keeps the Flask app and the Cloudflare tunnel alive, but
' it only lives as long as its host process. Run from a console window,
' that means closing the window takes the whole site down - which is
' exactly what kept happening, including one outage that lasted three
' days before anyone noticed.
'
' WScript.Shell.Run with intWindowStyle 0 launches PowerShell with no
' window at all, so there is nothing to close by accident. A copy of this
' file in the Startup folder makes it survive a reboot too, and unlike a
' scheduled task it needs no administrator rights to install.
'
' To stop it: Task Manager -> Details -> end powershell.exe running
' serve.ps1, or run scripts\stop.ps1.
Option Explicit

Dim shell, fso, here, script
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

' Resolve serve.ps1 relative to this file, so moving the project folder
' doesn't silently break startup.
here = fso.GetParentFolderName(WScript.ScriptFullName)
script = fso.BuildPath(here, "serve.ps1")

If Not fso.FileExists(script) Then
    MsgBox "Cannot find serve.ps1 at:" & vbCrLf & script, 16, "RandomGenerals"
    WScript.Quit 1
End If

' 0 = hidden, False = don't wait for it to finish.
shell.Run "powershell.exe -ExecutionPolicy Bypass -WindowStyle Hidden -File """ & script & """", 0, False
