# UxPlay TV – Home Assistant Add-on

iPhone'un **Ekran Yansıtma (AirPlay)** özelliğini, AirPlay desteği olmayan ama **DLNA** ile video oynatabilen TV'lere (örn. LG 47LA652V / NetCast) taşır.

```
iPhone ─AirPlay─► UxPlay ─RTP(H.264 + PCM)─► ffmpeg ─MPEG-TS─► HTTP :8090/stream.ts ─DLNA─► TV
                                                                      ▲
                                      HA: media_player.play_media (otomatik)
```

## Kurulum

1. Ayarlar → Eklentiler → Eklenti Mağazası → ⋮ → Depolar → `https://github.com/Korkuttum/ha-uxplay-tv` ekleyin.
2. **UxPlay AirPlay -> DLNA TV** eklentisini kurun ve başlatın (ilk derleme Pi'de uzun sürebilir).
3. Kenar çubuğundaki **UxPlay TV** panelini (veya eklentideki "Arayüzü aç" düğmesini) açın.
4. Listeden TV'nizi (medya oynatıcı) seçin. AirPlay adı olarak TV'nin adı otomatik gelir, isterseniz değiştirin. **Kaydet**.
5. iPhone'da Denetim Merkezi → Ekran Yansıtma → listedeki adı seçin.

Başka bir ayar gerekmez. Akış adresi otomatik bulunur (`http://<pi-ip>:8090/stream.ts`).

## Ayarlar

Tüm ayarlar panelden yapılır. Eklentinin Yapılandırma sekmesinde yalnızca `debug` (ayrıntılı log) vardır.
Panelde "Gelişmiş" altında **Akış MIME türü** bulunur. TV akışı reddederse `video/mp2t` veya `video/vnd.dlna.mpeg-tts` deneyin.

## Elle test

Yayın yokken `http://<pi-ip>:8090/health` JSON durumunu verir. `http://<pi-ip>:8090/stream.ts` adresini VLC'de açıp sonra iPhone'dan yansıtarak akışı TV'siz de görebilirsiniz.

## Bilinen sınırlar

- Gecikme TV tamponuna bağlı olarak 2–6 sn. Oyun için uygun değil.
- DRM'li içerik (Netflix vb.) yansımaz.
- iPhone ekranı sabitken video karesi gönderilmez; yayını bitmiş saymak için bekleme süresi bu yüzden varsayılan 30 sn. Çok kısa yaparsanız durağan ekran yayını kapanabilir.
- Add-on kendi Avahi (mDNS) servisini çalıştırır; ağınızda mDNS engelli ise iPhone cihazı göremez.
- Yayın TV'ye ortadan bağlandığı için ilk saniyede görüntü bir sonraki anahtar kareye kadar bozuk/boş olabilir.

> Durum: akış katmanı (RTP→MPEG-TS→HTTP) sentetik kaynakla test edildi. UxPlay + Avahi + gerçek iPhone + gerçek TV birleşimi henüz denenmedi.

UxPlay: <https://github.com/FDH2/UxPlay> (GPL-3.0)
