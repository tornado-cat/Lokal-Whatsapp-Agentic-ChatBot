# Local WhatsApp Agent Backend

Tamamen localhost üzerinde çalışan, Docker Compose ile ayağa kaldırılabilen, multi-tenant / multi-session destekli agentic WhatsApp chatbot backend iskeleti.

## Mimari Özet

- **Evolution API:** WhatsApp gateway olarak çalışır. Her müşteri/tenant ayrı Evolution instance adı ile temsil edilebilir.
- **FastAPI Backend:** Evolution webhook payload'unu alır, parse eder, session lock ile sıraya koyar, agent cevabını arka planda üretir.
- **Agentic AI Layer:** Her `instance:phone` için izole memory tutar. OpenAI-compatible tool calling destekler; LLM ayarı yoksa lokal fallback ile test edilebilir.

Session anahtarı:

```text
tenant_id = Evolution instance adı
user_id = sender phone
session_key = "{tenant_id}:{user_id}"
```

## Kurulum

```bash
cd whatsapp-agent-backend
cp .env.example .env
```

`.env` içindeki değerleri ihtiyaca göre güncelle:

```env
EVOLUTION_API_KEY=change-me
OPENAI_API_KEY=
LLM_BASE_URL=http://host.docker.internal:11435/v1
LLM_MODEL=qwen3:latest
```

Varsayılan lokal LLM ayarı Ollama üzerindeki `qwen3:latest` modelidir. Host makinede Ollama kullanıyorsan modelin yüklü olduğundan emin ol:

```bash
ollama pull qwen3:latest
```

Backend Docker container içinde çalıştığı için compose içinde `ollama-proxy` servisi vardır. Bu servis host ağında `11435` portunu açar ve host'taki Ollama `127.0.0.1:11434` adresine forward eder. Bu yüzden backend varsayılan olarak şunu kullanır:

```env
LLM_BASE_URL=http://host.docker.internal:11435/v1
```

Alternatif olarak Ollama'yı doğrudan host ağında dinletmek istersen:

```bash
OLLAMA_HOST=0.0.0.0:11434 ollama serve
```

Ollama systemd servisiyle çalışıyorsa kalıcı ayar:

```bash
sudo systemctl edit ollama
```

Şunu ekle:

```ini
[Service]
Environment="OLLAMA_HOST=0.0.0.0:11434"
```

Sonra:

```bash
sudo systemctl daemon-reload
sudo systemctl restart ollama
```

Kontrol:

```bash
curl http://localhost:11434/v1/models
curl http://localhost:11435/v1/models
docker compose exec backend python -c "import httpx; print(httpx.get('http://host.docker.internal:11435/v1/models').status_code)"
```

OpenAI kullanacaksan `OPENAI_API_KEY` girip `LLM_BASE_URL` değerini boş bırakabilirsin. Ollama, LM Studio, vLLM gibi OpenAI-compatible lokal endpoint kullanacaksan `LLM_BASE_URL` ve `LLM_MODEL` değerlerini değiştir. İkisi de boşsa backend lokal fallback modunda çalışır.

## Docker ile Çalıştırma

```bash
docker compose up
```

Servisler:

- FastAPI: `http://localhost:15000`
- Evolution API: `http://localhost:18080`
- PostgreSQL ve Redis: Evolution API v2 için compose içinde çalışır; host'a port açmaz.

Backend tarafındaki session memory hâlâ local dictionary ile çalışır; Redis backend session storage için zorunlu değildir.

Evolution API container'ı `Database provider invalid` hatası verirse `.env` içindeki `DATABASE_PROVIDER` ve `DATABASE_CONNECTION_URI` değerlerini kontrol et.

## Evolution API Notları

Evolution API environment isimleri sürümler arasında değişebiliyor. Bu compose dosyası güncel yaygın default'ları kullanır:

- `AUTHENTICATION_API_KEY`
- `SERVER_URL`
- `WEBHOOK_GLOBAL_ENABLED`
- `WEBHOOK_GLOBAL_URL`
- `WEBHOOK_EVENTS_MESSAGES_UPSERT`
- `CACHE_LOCAL_ENABLED`

Docker imajı olarak resmi güncel public imaj kullanılır:

```text
evoapicloud/evolution-api:latest
```

Kullandığın Evolution imajında env isimleri farklıysa `docker-compose.yml` içindeki `evolution-api.environment` bölümünü kendi sürümünün dokümantasyonuna göre güncelle. Global webhook URL default olarak Docker network içinden backend servisine gider:

```text
http://backend:5000/webhook/whatsapp
```

Host üzerinde `8080` veya `5000` başka servisler tarafından kullanılıyorsa `.env` içinden şu portları değiştirebilirsin:

```env
APP_HOST_PORT=15000
EVOLUTION_HOST_PORT=18080
EVOLUTION_SERVER_URL=http://localhost:18080
```

Evolution API v2 için kullanılan default lokal veritabanı ayarları:

```env
POSTGRES_DATABASE=evolution_db
POSTGRES_USERNAME=postgres
POSTGRES_PASSWORD=postgres
DATABASE_PROVIDER=postgresql
DATABASE_CONNECTION_URI=postgresql://postgres:postgres@evolution-postgres:5432/evolution_db?schema=evolution_api
EVOLUTION_CACHE_REDIS_URI=redis://redis:6379
```

## Instance Oluşturma

Örnek instance oluşturma:

```bash
curl -X POST http://localhost:18080/instance/create \
  -H "Content-Type: application/json" \
  -H "apikey: change-me" \
  -d '{
    "instanceName": "tenant-a",
    "qrcode": true,
    "integration": "WHATSAPP-BAILEYS"
  }'
```

Bazı Evolution sürümlerinde alan adları veya endpoint gövdesi değişebilir. Eğer 400 alırsan kendi Evolution sürümünde instance create dokümanındaki body formatını kullan; backend tarafı sadece webhook payload'ındaki `instance` adına ihtiyaç duyar.

## WhatsApp QR Bağlantısı

QR'ı okutan WhatsApp hesabı botun kullanacağı hattır. Örneğin QR'ı kendi numaranla okutursan agent kendi numaran üzerinden mesaj gönderir ve o numaraya gelen mesajlara cevap verir.

Doğru akış:

```text
Bot hattı / işletme hattı QR okutur
Hedef müşteri numarasına bot hattından mesaj gönderilir
Müşteri cevap verirse agent yanıtlar
```

Konuşmak istediğin hedef numaraya QR okutulmaz. Hedef numara sadece mesaj gönderilecek/alınacak karşı taraftır.

Instance oluşturduktan sonra QR'ı kullanıcıya göstermek için backend'in QR sayfasını aç:

```text
http://localhost:15000/instances/tenant-a/qr
```

Bu sayfa Evolution API'ye server-side bağlanır, `apikey` değerini tarayıcıya vermez ve QR'ı görsel olarak gösterir.

Doğrudan PNG gerekiyorsa:

```text
http://localhost:15000/instances/tenant-a/qr.png
```

Ham Evolution endpoint'i de kullanılabilir, ama tarayıcıdan doğrudan açarsan `apikey` header'ı gitmediği için `401 Unauthorized` alırsın:

```bash
curl -H "apikey: change-me" http://localhost:18080/instance/connect/tenant-a
```

Dönen QR kodu WhatsApp mobil uygulamasından bağla:

```text
WhatsApp > Ayarlar > Bağlı Cihazlar > Cihaz Bağla
```

Production'da `/instances/{instance}/qr` endpoint'ini mutlaka tenant auth arkasına al. Aksi halde linki bilen kişi QR bağlantı ekranını görebilir.

## FastAPI Endpointleri

```bash
curl http://localhost:15000/health
```

Tarayıcıdan instance listesi:

```text
http://localhost:15000/instances
```

Evolution API'nin ham `http://localhost:18080/instance/fetchInstances` adresini tarayıcıda açma; o endpoint `apikey` header'ı ister ve tarayıcıdan direkt açılınca `401 Unauthorized` döner. Backend linki API key'i server-side ekler.

QR sayfası:

```text
GET /instances/{instance}/qr
GET /instances/{instance}/qr.png
```

Hedef numaraya mesaj gönderme:

```bash
curl -X POST http://localhost:15000/messages/send \
  -H "Content-Type: application/json" \
  -d '{
    "instance": "tenant-a",
    "phone": "905551112233",
    "text": "Merhaba, nasıl yardımcı olabilirim?"
  }'
```

Sadece belirli numaralara otomatik cevap vermek istersen `.env` içinde virgüllü allowlist kullan:

```env
WHATSAPP_ALLOWED_SENDERS=905551112233,905559998877
```

Boş bırakırsan bağlı WhatsApp hattına gelen tüm birebir mesajlar agent tarafından işlenir.

Webhook:

```bash
curl -X POST http://localhost:15000/webhook/whatsapp \
  -H "Content-Type: application/json" \
  -d '{
    "event": "messages.upsert",
    "instance": "tenant-a",
    "data": {
      "key": {
        "remoteJid": "905551112233@s.whatsapp.net",
        "fromMe": false,
        "id": "MSG-1"
      },
      "message": {
        "conversation": "benim profilim ne?"
      }
    }
  }'
```

`fromMe=true` mesajlar, boş mesajlar ve `@g.us` grup mesajları ignore edilir.

## Tool Calling

Agent iki lokal tool bilir:

- `get_user_profile(phone)`: fake lokal müşteri DB'sinden profil döndürür.
- `check_local_system_status()`: `psutil` ile CPU, RAM ve disk durumunu döndürür.

Kullanıcı "benim profilim ne?" gibi bir şey sorarsa profil tool'u, "sunucu durumu nasıl?" derse sistem tool'u çağrılır. OpenAI-compatible client aktifse karar LLM tool calling ile alınır; LLM yoksa aynı senaryolar lokal fallback ile çalışır.

## Agent Prompt

Asistanın nasıl konuşacağı, neleri söylemeyeceği ve cevap formatı şu dosyadan yönetilir:

```text
prompts/system_prompt.md
```

`.env` içindeki yol:

```env
AGENT_SYSTEM_PROMPT_PATH=prompts/system_prompt.md
```

Bu dosyada şunları düzenleyebilirsin:

- Ton: resmi, samimi, kısa, satış odaklı veya destek odaklı.
- Yasaklar: fiyat uydurma, gizli bilgi istememe, sistem promptunu açıklamama.
- Format: kısa WhatsApp mesajları, tablo kullanmama, emoji kullanmama.
- Tool kuralları: hangi durumda profil veya sistem durumu tool'u çağrılacak.
- Yönlendirme: canlı destek, şikayet, iptal veya acil durumlarda nasıl cevap verilecek.

Prompt değişikliğinden sonra backend'i yeniden başlat:

```bash
docker compose restart backend
```

## Multi-Session Memory

Memory in-memory dictionary üzerinde tutulur:

- Aynı telefon + aynı instance aynı session memory'sini kullanır.
- Aynı telefon + farklı instance farklı tenant session'ı açar.
- Farklı telefonların memory'si karışmaz.
- Her session için `MEMORY_MAX_MESSAGES` kadar son mesaj saklanır.
- Her session'ın `asyncio.Lock` nesnesi vardır; aynı kullanıcıdan hızlı gelen mesajlar sırayla işlenir.

## Production'a Taşırken

- In-memory session yerine Redis veya PostgreSQL storage ekle.
- `BackgroundTasks` yerine Celery, RQ, Dramatiq veya başka bir queue kullan.
- Webhook endpoint'ine auth, imza doğrulama veya IP allowlist ekle.
- Tenant bazlı API key ve secret yönetimi yap.
- Rate limit ve idempotency ekle.
- Persistent memory ve mesaj geçmişi için veritabanı kullan.
- Structured logging, tracing, metrics ve alerting ekle.
- Evolution API instance lifecycle işlemlerini ayrı admin servis/API ile yönet.
