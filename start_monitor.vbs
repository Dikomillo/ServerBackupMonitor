Set shell = CreateObject("WScript.Shell")
Set files = CreateObject("Scripting.FileSystemObject")
folder = files.GetParentFolderName(WScript.ScriptFullName)
pyw = shell.ExpandEnvironmentStrings("%SystemRoot%") & "\pyw.exe"
shell.CurrentDirectory = folder
If files.FileExists(pyw) Then
  command = Chr(34) & pyw & Chr(34) & " -3 "
Else
  command = ""
  pythonRoot = shell.ExpandEnvironmentStrings("%LocalAppData%") & "\Programs\Python"
  If files.FolderExists(pythonRoot) Then
    For Each candidateFolder In files.GetFolder(pythonRoot).SubFolders
      candidate = candidateFolder.Path & "\pythonw.exe"
      If files.FileExists(candidate) Then command = Chr(34) & candidate & Chr(34) & " "
    Next
  End If
End If
If command = "" Then
  MsgBox "Python 3 не найден. Установите Python с python.org.", 16, "Server Backup Monitor"
Else
  shell.Run command & Chr(34) & folder & "\backup_monitor.py" & Chr(34) & " --gui", 0, False
End If
