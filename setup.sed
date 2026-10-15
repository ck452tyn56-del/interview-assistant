; SED-файл IExpress: самораспаковывающийс€ установщик Fast Assistant
[Version]
Class=IEXPRESS
SEDVersion=3
[Options]
PackagePurpose=InstallApp
ShowInstallProgramWindow=0
HideExtractAnimation=1
UseLongFileName=1
InsideCompressed=0
CAB_FixedSize=0
CAB_ResvCodeSigning=0
RebootMode=N
InstallPrompt=
DisplayLicense=
FinishMessage=”становка Fast Assistant завершена! ярлык - на рабочем столе.
TargetName=C:\Users\XXX\Desktop\FastAssistant-Setup.exe
FriendlyName=Fast Assistant Setup
AppLaunched=powershell -NoProfile -ExecutionPolicy Bypass -File install.ps1
PostInstallCmd=<None>
AdminQuietInstCmd=
UserQuietInstCmd=
SourceFiles=SourceFiles
[Strings]
FILE0="FastAssistant.exe"
FILE1="assistant_secrets.py"
FILE2="install.ps1"
[SourceFiles]
SourceFiles0=C:\Users\XXX\.zcode\workspace\default\interview-assistant\windows-installer
[SourceFiles0]
%FILE0%=
%FILE1%=
%FILE2%=
