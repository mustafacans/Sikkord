SIKKORD PERFORMANCE V7

Bu sürüm özellikle donma/kasma ve ses takılmasını azaltmak için hazırlanmıştır.

ANA DEĞİŞİKLİKLER
- PortAudio callback içinde ağır DSP kaldırıldı.
- Mikrofon ve hoparlör blocking read/write worker thread'lerine taşındı.
- Gürültü engelleme tamamen vektörize edildi.
- Ses paketlerinde eski paket birikimi engellenmeye devam ediyor.
- Login sırasında gereksiz voice + screen WebSocket bağlantıları açılmıyor.
- Media bağlantıları ihtiyaç olduğunda bir kere başlatılıyor.
- Sunucu geçmişinde avatar base64 her mesajda tekrar gönderilmiyor.
- Eski resim/dosya base64 verileri geçmiş yüklenirken gönderilmiyor.
- Dosya/resim yalnız tıklanınca PostgreSQL'den isteniyor.
- Chat geçmişi 10 mesajlık küçük UI batch'leriyle çiziliyor.
- Avatarlar istemci tarafında cache'leniyor.
- Sunucu ve DM geçmişi istemcide cache'leniyor; geri dönünce anında gösteriliyor.
- DM okundu güncellemesi artık bütün chat'i tekrar çizdirmiyor.
- Arama katılımcı ekranı yalnız katılımcı durumu değişirse yeniden çiziliyor.
- Screen share varsayılanı 720p / 6 FPS akıcı moda çekildi.
- Sunucu read receipt sorgusu yalnız son 100 mesajı işler.

KURULUM
1. GitHub: client.py ve server.py dosyalarını bu sürümle değiştir.
2. Render: Manual Deploy > Clear build cache & deploy.
3. Windows: build_exe.bat ile yeni EXE oluştur.
4. Sen ve arkadaşların aynı yeni EXE'yi kullanmalı.
