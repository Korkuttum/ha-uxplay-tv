# UxPlay TV – Home Assistant Add-on

iPhone'un **Ekran Yansıtma (AirPlay)** özelliğini, AirPlay desteği olmayan ama **DLNA** ile video oynatabilen TV'lere (örn. LG 47LA652V / NetCast) taşır.

```
iPhone ─AirPlay─► UxPlay ─RTP(H.264 + PCM)─► ffmpeg ─MPEG-TS─► HTTP :8090/stream.ts ─DLNA─► TV
                                                                      ▲
                                      HA: media_player.play_media (otomatik)
```

## Kurulum

1. Ayarlar → Eklentiler → Eklenti Mağazası → ⋮ → Depolar → bu reponun adresini ekleyin
   (repo özel ise bunun yerine `uxplay_tv` klasörünü HA'daki `/addons/uxplay_tv/` içine kopyalayıp "Yerel eklentiler"den kurun).
2. **UxPlay AirPlay -> DLNA TV** eklentisini kurun (ilk derleme Pi'de birkaç dakika sürer).
3. Yapılandırmada en az `tv_entity` (örn. `media_player.lg_tv`) alanını doldurun.
4. Eklentiyi başlatın. iPhone'da Denetim Merkezi → Ekran Yansıtma → `airplay_name` ile verdiğiniz isme dokunun.

## Ayarlar

| Ayar | Açıklama |
|---|---|
| `airplay_name` | iPhone'da görünecek cihaz adı |
| `tv_entity` | HA'daki DLNA TV varlığı (boşsa otomatik oynatma kapalı) |
| `stream_host` | TV'nin ulaşacağı Pi IP'si; boşsa otomatik bulunur |
| `stream_port` | HTTP akış portu (varsayılan 8090) |
| `media_content_type` | `play_media` MIME türü. TV reddederse `video/mp2t` veya `video/vnd.dlna.mpeg-tts` deneyin |
| `resolution`, `fps` | iPhone'a bildirilen yayın boyutu / kare hızı |
| `audio_bitrate` | AAC ses bit hızı |
| `idle_timeout` | Bu kadar sn veri gelmezse yayın bitmiş sayılır |
| `auto_play` | Yayın başlayınca TV'ye otomatik gönder |
| `stop_tv_on_end` | Yayın bitince TV'yi durdur |
| `debug` | Ayrıntılı log |

## Elle test

Yayın yokken `http://<pi-ip>:8090/health` JSON durumunu verir. `http://<pi-ip>:8090/stream.ts` adresini VLC'de açıp sonra iPhone'dan yansıtarak akışı TV'siz de görebilirsiniz.

## Bilinen sınırlar

- Gecikme TV tamponuna bağlı olarak 2–6 sn. Oyun için uygun değil.
- DRM'li içerik (Netflix vb.) yansımaz.
- iPhone ekranı sabitken video karesi gönderilmez; `idle_timeout` bu yüzden varsayılan 20 sn. Çok kısa yaparsanız durağan ekran yayını kapanabilir.
- Add-on kendi Avahi (mDNS) servisini çalıştırır; ağınızda mDNS engelli ise iPhone cihazı göremez.
- Yayın TV'ye ortadan bağlandığı için ilk saniyede görüntü bir sonraki anahtar kareye kadar bozuk/boş olabilir.

> Durum: akış katmanı (RTP→MPEG-TS→HTTP) sentetik kaynakla test edildi. UxPlay + Avahi + gerçek iPhone + gerçek TV birleşimi henüz denenmedi.

UxPlay: <https://github.com/FDH2/UxPlay> (GPL-3.0)
