# 音频容器测试样本

`silence.flac` 是本地生成的三秒静音音频：44.1 kHz、16 bit、双声道，移除了音频标签和填充区。
它不含第三方录音内容，用于离线验证 Mutagen 对真实无标签 FLAC 的流参数读取。
测试先复制到临时目录，再写入标签，不修改此原始样本，也不要求 CI 安装 ffmpeg。

生成过程：标准库 `wave` 写入三秒零值 PCM，通过 ffmpeg 的 `flac` 编码器转换，再使用
`mutagen.flac.FLAC.clear()` 和 `save(padding=lambda _: 0)` 清除标签及填充。

`silence.m4a`（ALAC）与 `silence.mp3`（32 kbps MP3）由同一静音 FLAC 本地转换，
移除了输入元数据，用于验证原生 MP4 atom/freeform 与 MP3 ID3 字段。
测试直接通过 Mutagen 写入独立 MBID 和日期，CI 不依赖编码器。
M4A 的 ORIGINALDATE 是可选自定义 freeform 兼容样本，不声明它是 MP4 标准日期字段。
