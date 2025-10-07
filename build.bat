@echo off
echo Lancement de la compilation de l'executable...

rem Installe les dépendances au cas où elles manqueraient
pip install -r requirements.txt

rem Lance la compilation avec le fichier de configuration .spec
pyinstaller main.spec --noconfirm

echo.
echo ===================================================================
echo Compilation terminee.
echo Le nouvel executable 'main.exe' se trouve dans le dossier 'dist'.
echo ===================================================================
echo.
pause
