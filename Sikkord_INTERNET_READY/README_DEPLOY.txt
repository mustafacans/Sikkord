SIKKORD INTERNET READY

Render ile test sunucusu:
1) GitHub'da yeni repo oluştur ve bu klasördeki dosyaları yükle.
2) Render -> New -> Web Service -> GitHub repo seç.
3) Build Command: pip install -r requirements.txt
4) Start Command: python server.py
5) Free plan seç.
6) Deploy bitince https://...onrender.com adresini al.

Ardından client.py içindeki SERVER_URL değerini
wss://SENIN-ADRESIN.onrender.com
olarak değiştir.
Sonra build_exe.bat çalıştır; dist/Sikkord.exe arkadaşına gönder.

Not: Render Free uykuya geçebilir ve yerel SQLite kalıcı değildir. Arkadaşlarla test içindir.
