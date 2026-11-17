' Liga o leitor de placas sem janela. O instalar.bat poe um atalho para este arquivo no inicio do Windows.
Set fso = CreateObject("Scripting.FileSystemObject")
pasta = fso.GetParentFolderName(WScript.ScriptFullName)
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = pasta
sh.Run """" & pasta & "\.venv\Scripts\pythonw.exe"" """ & pasta & "\leitor.py"" --escondido", 0, False
