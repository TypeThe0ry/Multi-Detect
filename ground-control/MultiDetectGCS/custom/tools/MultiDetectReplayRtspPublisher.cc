#include "MultiDetectReplayRtspWire.h"
#include "MultiDetectReplayRtspGeneration.h"

#include <gst/app/gstappsrc.h>
#include <gst/gst.h>
#include <gst/rtsp-server/rtsp-server.h>

#ifdef _WIN32
#include <fcntl.h>
#include <io.h>
#endif

#include <algorithm>
#include <array>
#include <atomic>
#include <charconv>
#include <chrono>
#include <condition_variable>
#include <cstdint>
#include <cstring>
#include <iostream>
#include <limits>
#include <mutex>
#include <new>
#include <optional>
#include <sstream>
#include <string>
#include <string_view>
#include <system_error>
#include <thread>
#include <type_traits>
#include <utility>
#include <vector>

namespace {

using MultiDetectReplayRtsp::Message;
using MultiDetectReplayRtsp::MessageType;

constexpr std::string_view kEventSchema = "multidetect.replay-rtsp.event";
constexpr std::uint32_t kEventSchemaVersion = 1;
constexpr std::uint32_t kDefaultClientTimeoutMs = 15'000;
constexpr std::uint32_t kMaximumClientTimeoutMs = 300'000;
constexpr std::uint32_t kDefaultMinimumClientCount = 1;
constexpr std::uint32_t kMaximumMinimumClientCount = 64;
constexpr std::uint32_t kEosDrainMs = 250;
constexpr std::uint32_t kMaximumGenerationRetriesPerMessage = 8;

std::mutex gEventMutex;

[[nodiscard]] std::string jsonEscape(std::string_view value)
{
    std::ostringstream output;
    for (const unsigned char character : value) {
        switch (character) {
        case '"':
            output << "\\\"";
            break;
        case '\\':
            output << "\\\\";
            break;
        case '\b':
            output << "\\b";
            break;
        case '\f':
            output << "\\f";
            break;
        case '\n':
            output << "\\n";
            break;
        case '\r':
            output << "\\r";
            break;
        case '\t':
            output << "\\t";
            break;
        default:
            if (character < 0x20U) {
                constexpr char hex[] = "0123456789abcdef";
                output << "\\u00" << hex[(character >> 4U) & 0x0FU] << hex[character & 0x0FU];
            } else {
                output << static_cast<char>(character);
            }
        }
    }
    return output.str();
}

class JsonEvent {
public:
    explicit JsonEvent(std::string_view event)
    {
        string("schema", kEventSchema);
        number("schema_version", kEventSchemaVersion);
        number("version", kEventSchemaVersion);
        string("event", event);
    }

    void string(std::string_view key, std::string_view value)
    {
        fieldPrefix(key);
        _stream << '"' << jsonEscape(value) << '"';
    }

    template<typename T>
    void number(std::string_view key, T value)
    {
        fieldPrefix(key);
        _stream << value;
    }

    void boolean(std::string_view key, bool value)
    {
        fieldPrefix(key);
        _stream << (value ? "true" : "false");
    }

    [[nodiscard]] std::string finish()
    {
        return '{' + _stream.str() + '}';
    }

private:
    void fieldPrefix(std::string_view key)
    {
        if (!_first) {
            _stream << ',';
        }
        _first = false;
        _stream << '"' << jsonEscape(key) << "\":";
    }

    std::ostringstream _stream;
    bool _first = true;
};

void emitEvent(JsonEvent event)
{
    std::lock_guard lock(gEventMutex);
    std::cout << event.finish() << '\n' << std::flush;
}

[[nodiscard]] std::uint64_t monotonicNowNs()
{
    return static_cast<std::uint64_t>(
        std::chrono::duration_cast<std::chrono::nanoseconds>(std::chrono::steady_clock::now().time_since_epoch())
            .count());
}

struct Options {
    std::string bindAddress = "127.0.0.1";
    std::string mount = "/multidetect-replay";
    std::uint16_t port = 0;
    std::uint32_t width = 0;
    std::uint32_t height = 0;
    std::uint32_t clientTimeoutMs = kDefaultClientTimeoutMs;
    std::uint32_t minimumClientCount = kDefaultMinimumClientCount;
};

template<typename T>
[[nodiscard]] bool parseUnsigned(std::string_view text, T& value)
{
    static_assert(std::is_unsigned_v<T>);
    if (text.empty()) {
        return false;
    }
    T parsed = 0;
    const auto [end, error] = std::from_chars(text.data(), text.data() + text.size(), parsed);
    if (error != std::errc{} || end != text.data() + text.size()) {
        return false;
    }
    value = parsed;
    return true;
}

void printUsage()
{
    std::cerr
        << "Usage: MultiDetectReplayRtspPublisher --port PORT --width PIXELS --height PIXELS\n"
           "       [--bind 127.0.0.1] [--mount /multidetect-replay]\n"
           "       [--client-timeout-ms 1..300000]\n"
           "       [--minimum-client-count 1..64]\n";
}

[[nodiscard]] bool parseOptions(int argc, char* argv[], Options& options, std::string& error)
{
    for (int index = 1; index < argc; ++index) {
        const std::string_view argument(argv[index]);
        if (argument == "--help" || argument == "-h") {
            printUsage();
            error.clear();
            return false;
        }
        if (index + 1 >= argc) {
            error = "missing value for " + std::string(argument);
            return false;
        }
        const std::string_view value(argv[++index]);
        if (argument == "--bind") {
            options.bindAddress = value;
        } else if (argument == "--mount") {
            options.mount = value;
        } else if (argument == "--port") {
            if (!parseUnsigned(value, options.port) || options.port == 0) {
                error = "--port must be in the range 1..65535";
                return false;
            }
        } else if (argument == "--width") {
            if (!parseUnsigned(value, options.width)) {
                error = "--width must be an unsigned integer";
                return false;
            }
        } else if (argument == "--height") {
            if (!parseUnsigned(value, options.height)) {
                error = "--height must be an unsigned integer";
                return false;
            }
        } else if (argument == "--client-timeout-ms") {
            if (!parseUnsigned(value, options.clientTimeoutMs) || options.clientTimeoutMs == 0
                || options.clientTimeoutMs > kMaximumClientTimeoutMs) {
                error = "--client-timeout-ms must be in the range 1..300000";
                return false;
            }
        } else if (argument == "--minimum-client-count") {
            if (!parseUnsigned(value, options.minimumClientCount) || options.minimumClientCount == 0
                || options.minimumClientCount > kMaximumMinimumClientCount) {
                error = "--minimum-client-count must be in the range 1..64";
                return false;
            }
        } else {
            error = "unknown option: " + std::string(argument);
            return false;
        }
    }

    if (options.port == 0 || options.width == 0 || options.height == 0) {
        error = "--port, --width and --height are required";
        return false;
    }
    if (options.bindAddress != "127.0.0.1") {
        error = "--bind is fail-closed to the exact loopback address 127.0.0.1";
        return false;
    }
    if (!MultiDetectReplayRtsp::validRtspMount(options.mount, error)) {
        error = "invalid --mount: " + error;
        return false;
    }
    return MultiDetectReplayRtsp::validH264Geometry(options.width, options.height, error);
}

[[nodiscard]] bool factoryExists(const char* name)
{
    GstElementFactory* factory = gst_element_factory_find(name);
    if (factory == nullptr) {
        return false;
    }
    gst_object_unref(factory);
    return true;
}

[[nodiscard]] std::optional<std::string> chooseEncoder(std::string& launchFragment)
{
    if (factoryExists("openh264enc")) {
        launchFragment = "openh264enc usage-type=camera complexity=low rate-control=bitrate bitrate=4000000 "
                         "gop-size=30 enable-frame-skip=false";
        return "openh264enc";
    }
    if (factoryExists("x264enc")) {
        launchFragment = "x264enc tune=zerolatency speed-preset=ultrafast sliced-threads=true bframes=0 "
                         "key-int-max=30 bitrate=4000 byte-stream=true";
        return "x264enc";
    }
    return std::nullopt;
}

class Publisher {
public:
    explicit Publisher(Options options)
        : _options(std::move(options))
        , _validator(_options.width, _options.height)
    {
    }

    ~Publisher()
    {
        shutdownAppSrc();
        if (_sourceId != 0) {
            g_source_remove(_sourceId);
            _sourceId = 0;
        }
        if (_server != nullptr) {
            g_object_unref(_server);
            _server = nullptr;
        }
        if (_loop != nullptr) {
            g_main_loop_unref(_loop);
            _loop = nullptr;
        }
    }

    [[nodiscard]] bool start(std::string& error)
    {
        static constexpr std::array<const char*, 4> requiredFactories{
            "appsrc", "videoconvert", "h264parse", "rtph264pay"};
        for (const char* factory : requiredFactories) {
            if (!factoryExists(factory)) {
                error = "required GStreamer element is unavailable: " + std::string(factory);
                return false;
            }
        }

        std::string encoderLaunch;
        const std::optional<std::string> encoder = chooseEncoder(encoderLaunch);
        if (!encoder.has_value()) {
            error = "neither openh264enc nor x264enc is available";
            return false;
        }
        _encoder = *encoder;

        _loop = g_main_loop_new(nullptr, FALSE);
        if (_loop == nullptr) {
            error = "failed to create the GLib main loop";
            return false;
        }

        _server = gst_rtsp_server_new();
        if (_server == nullptr) {
            error = "failed to allocate GstRTSPServer";
            return false;
        }
        const std::string service = std::to_string(_options.port);
        g_object_set(_server, "address", _options.bindAddress.c_str(), "service", service.c_str(), nullptr);
        g_signal_connect(_server, "client-connected", G_CALLBACK(&Publisher::clientConnectedCallback), this);

        GstRTSPMediaFactory* factory = gst_rtsp_media_factory_new();
        if (factory == nullptr) {
            error = "failed to allocate GstRTSPMediaFactory";
            return false;
        }

        std::ostringstream launch;
        launch << "( appsrc name=source is-live=true block=true format=time do-timestamp=false "
               << "caps=\"video/x-raw,format=(string)BGR,width=(int)" << _options.width << ",height=(int)"
               // RTSP SDP requires a concrete, positive nominal frame rate
               // during DESCRIBE/SETUP.  The replay wire timestamp remains
               // authoritative for PTS/DTS below; this value only makes the
               // live H.264 caps negotiable for receivers before the first
               // replay frame arrives.
               << _options.height << ",framerate=(fraction)30/1\" "
               << "! queue max-size-buffers=4 max-size-bytes=0 max-size-time=0 leaky=no "
               << "! videoconvert n-threads=1 ! video/x-raw,format=(string)I420 "
               << "! " << encoderLaunch << " ! h264parse config-interval=-1 "
               << "! rtph264pay name=pay0 pt=96 config-interval=1 aggregate-mode=zero-latency )";
        const std::string launchText = launch.str();
        gst_rtsp_media_factory_set_launch(factory, launchText.c_str());
        // A QGC receiver restart must obtain a fresh appsrc/pipeline instead
        // of inheriting EOS or flushing state from a disconnected shared
        // media instance.  Per-session media also gives every reconnect an
        // unambiguous generation identity for delayed cleanup callbacks.
        //
        // The explicit multi-client bench is the one exception: a shared
        // RTSP media pipeline is the native GStreamer fan-out mechanism and
        // avoids serialising several DESCRIBE handshakes behind one
        // non-shared encoder.  The normal single-client path stays
        // non-shared so a QGC reconnect always gets a fresh generation.
        _sharedFactory = _options.minimumClientCount > 1;
        gst_rtsp_media_factory_set_shared(factory, _sharedFactory ? TRUE : FALSE);
        gst_rtsp_media_factory_set_stop_on_disconnect(factory, TRUE);
        gst_rtsp_media_factory_set_eos_shutdown(factory, TRUE);
        gst_rtsp_media_factory_set_latency(factory, 0);
        gst_rtsp_media_factory_set_transport_mode(factory, GST_RTSP_TRANSPORT_MODE_PLAY);
        g_signal_connect(factory, "media-configure", G_CALLBACK(&Publisher::mediaConfigureCallback), this);

        GstRTSPMountPoints* mounts = gst_rtsp_server_get_mount_points(_server);
        if (mounts == nullptr) {
            g_object_unref(factory);
            error = "failed to acquire GstRTSPMountPoints";
            return false;
        }
        gst_rtsp_mount_points_add_factory(mounts, _options.mount.c_str(), factory);
        g_object_unref(mounts);

        _sourceId = gst_rtsp_server_attach(_server, nullptr);
        if (_sourceId == 0) {
            error = "failed to bind the RTSP server; the loopback port may already be in use";
            return false;
        }

        JsonEvent ready("server_ready");
        ready.string("bind", _options.bindAddress);
        ready.number("port", _options.port);
        ready.string("mount", _options.mount);
        ready.string("url", "rtsp://" + _options.bindAddress + ':' + service + _options.mount);
        ready.number("width", _options.width);
        ready.number("height", _options.height);
        ready.string("pixel_format", "BGR24");
        ready.string("encoder", _encoder);
        ready.number("client_timeout_ms", _options.clientTimeoutMs);
        ready.number("minimum_client_count", _options.minimumClientCount);
        ready.boolean("shared_media", _sharedFactory);
        emitEvent(std::move(ready));

        _reader = std::thread(&Publisher::readerLoop, this);
        g_main_loop_run(_loop);
        if (_reader.joinable()) {
            _reader.join();
        }
        return _exitCode.load() == 0;
    }

    [[nodiscard]] int exitCode() const { return _exitCode.load(); }

private:
    using AppSrcGeneration = MultiDetectReplayRtsp::AppSrcGenerationState::Generation;

    struct AppSrcLease {
        GstAppSrc* source = nullptr;
        AppSrcGeneration generation = 0;
        std::uint64_t clientAvailabilityGeneration = 0;
        std::uint64_t appSrcSetGeneration = 0;
    };

    struct AppSrcBatchLease {
        std::vector<AppSrcLease> sources;
        std::uint32_t clientCount = 0;
        std::uint32_t appSrcCount = 0;
        std::uint64_t clientAvailabilityGeneration = 0;
        std::uint64_t appSrcSetGeneration = 0;
    };

    struct AppSrcBinding {
        GstAppSrc* source = nullptr;
        AppSrcGeneration generation = 0;
    };

    struct MediaBinding {
        Publisher* publisher = nullptr;
        AppSrcGeneration generation = 0;
    };

    static void clientConnectedCallback(GstRTSPServer*, GstRTSPClient* client, gpointer userData)
    {
        auto* self = static_cast<Publisher*>(userData);
        {
            std::lock_guard lock(self->_appSrcMutex);
            if (self->_clientCount == 0) {
                ++self->_clientAvailabilityGeneration;
            }
            if (self->_clientCount < std::numeric_limits<std::uint32_t>::max()) {
                ++self->_clientCount;
            }
        }
        self->_appSrcCondition.notify_all();
        self->emitInitialClientReadyIfReady();
        g_signal_connect(client, "closed", G_CALLBACK(&Publisher::clientClosedCallback), self);
    }

    static void clientClosedCallback(GstRTSPClient*, gpointer userData)
    {
        auto* self = static_cast<Publisher*>(userData);
        std::vector<GstAppSrc*> sources;
        {
            std::lock_guard lock(self->_appSrcMutex);
            if (self->_clientCount > 0) {
                --self->_clientCount;
            }
            if (self->_clientCount == 0 && !self->_appSrcs.empty()) {
                // A non-shared RTSP factory creates one media/appsrc per
                // client.  End every active source when the last control
                // client leaves so each pipeline can unprepare promptly.
                sources.reserve(self->_appSrcs.size());
                for (const auto& binding : self->_appSrcs) {
                    if (binding.source != nullptr) {
                        sources.push_back(GST_APP_SRC(gst_object_ref(binding.source)));
                    }
                }
            }
        }
        self->_appSrcCondition.notify_all();
        for (GstAppSrc* source : sources) {
            static_cast<void>(gst_app_src_end_of_stream(source));
            gst_object_unref(source);
        }
    }

    static void mediaConfigureCallback(GstRTSPMediaFactory*, GstRTSPMedia* media, gpointer userData)
    {
        auto* self = static_cast<Publisher*>(userData);
        GstElement* mediaElement = gst_rtsp_media_get_element(media);
        if (mediaElement == nullptr) {
            self->fail("media_element", "RTSP media did not expose a pipeline element");
            return;
        }
        GstElement* source = gst_bin_get_by_name_recurse_up(GST_BIN(mediaElement), "source");
        gst_object_unref(mediaElement);
        if (source == nullptr || !GST_IS_APP_SRC(source)) {
            if (source != nullptr) {
                gst_object_unref(source);
            }
            self->fail("appsrc_lookup", "RTSP media did not contain the required appsrc named source");
            return;
        }

        // DESCRIBE/SETUP can complete before the receiver sends PLAY.  A
        // blocking appsrc would wait for downstream consumption during that
        // handshake and deadlock the first fan-out frame, especially when
        // several non-shared media sessions are negotiated concurrently.
        // Keep the queue bounded below, but let the RTSP state machine reach
        // PLAY so the frame barrier can observe real RTP delivery.
        g_object_set(source, "format", GST_FORMAT_TIME, "block", FALSE, "is-live", TRUE, nullptr);
        gst_app_src_set_stream_type(GST_APP_SRC(source), GST_APP_STREAM_TYPE_STREAM);
        gst_app_src_set_max_bytes(GST_APP_SRC(source), self->_validator.frameBytes() * 4ULL);
        const std::optional<AppSrcGeneration> generation = self->publishAppSrc(GST_APP_SRC(source));
        if (!generation.has_value()) {
            self->fail("generation_exhausted", "RTSP media generation counter was exhausted");
            return;
        }

        auto* binding = new (std::nothrow) MediaBinding{self, *generation};
        if (binding == nullptr) {
            self->retireAppSrc(*generation);
            self->fail("media_binding_allocate", "failed to allocate RTSP media generation binding");
            return;
        }
        const gulong handler = g_signal_connect_data(media, "unprepared",
                                                     G_CALLBACK(&Publisher::mediaUnpreparedCallback), binding,
                                                     &Publisher::destroyMediaBindingCallback,
                                                     G_CONNECT_DEFAULT);
        if (handler == 0) {
            delete binding;
            self->retireAppSrc(*generation);
            self->fail("media_unprepared_signal", "failed to bind RTSP media unprepared lifecycle signal");
        }
    }

    static void mediaUnpreparedCallback(GstRTSPMedia*, gpointer userData)
    {
        auto* binding = static_cast<MediaBinding*>(userData);
        if (binding != nullptr && binding->publisher != nullptr) {
            binding->publisher->retireAppSrc(binding->generation);
        }
    }

    static void destroyMediaBindingCallback(gpointer data, GClosure*)
    {
        delete static_cast<MediaBinding*>(data);
    }

    static gboolean quitLoopCallback(gpointer userData)
    {
        auto* self = static_cast<Publisher*>(userData);
        if (self->_loop != nullptr) {
            g_main_loop_quit(self->_loop);
        }
        return G_SOURCE_REMOVE;
    }

    static gboolean scheduleDrainCallback(gpointer userData)
    {
        g_timeout_add(kEosDrainMs, &Publisher::quitLoopCallback, userData);
        return G_SOURCE_REMOVE;
    }

    void emitInitialClientReadyIfReady()
    {
        AppSrcGeneration generation = 0;
        std::uint64_t clientAvailabilityGeneration = 0;
        std::uint64_t appSrcSetGeneration = 0;
        std::uint32_t clientCount = 0;
        std::size_t appSrcCount = 0;
        {
            std::lock_guard lock(_appSrcMutex);
            if (_initialClientReadyEmitted || _clientCount < _options.minimumClientCount || _appSrcs.empty()) {
                return;
            }
            _initialClientReadyEmitted = true;
            generation = _appSrcs.back().generation;
            clientAvailabilityGeneration = _clientAvailabilityGeneration;
            appSrcSetGeneration = _appSrcSetGeneration;
            clientCount = _clientCount;
            appSrcCount = _appSrcs.size();
        }

        // The Python IPC consumer has a strict event barrier: after the first
        // client_ready it expects exactly one frame_accepted per input frame.
        // Reconnect generations therefore swap silently and are identified on
        // the matching frame_accepted ACK instead of injecting an out-of-order
        // lifecycle event into stdout.
        JsonEvent event("client_ready");
        event.number("generation", generation);
        event.number("client_availability_generation", clientAvailabilityGeneration);
        event.number("client_count", clientCount);
        event.number("appsrc_set_generation", appSrcSetGeneration);
        event.number("appsrc_count", appSrcCount);
        event.number("minimum_client_count", _options.minimumClientCount);
        event.boolean("ready_for_minimum", _sharedFactory || appSrcCount >= _options.minimumClientCount);
        event.boolean("shared_media", _sharedFactory);
        event.number("ready_monotonic_ns", monotonicNowNs());
        emitEvent(std::move(event));
    }

    [[nodiscard]] std::optional<AppSrcGeneration> publishAppSrc(GstAppSrc* source)
    {
        std::uint64_t generation = 0;
        {
            std::lock_guard lock(_appSrcMutex);
            const std::optional<AppSrcGeneration> published = _appSrcGeneration.publish();
            if (!published.has_value()) {
                gst_object_unref(source);
                _appSrcCondition.notify_all();
                return std::nullopt;
            }
            try {
                _appSrcs.push_back(AppSrcBinding{source, *published});
            } catch (...) {
                gst_object_unref(source);
                _appSrcCondition.notify_all();
                return std::nullopt;
            }
            bumpAppSrcSetGenerationLocked();
            generation = *published;
        }
        _appSrcCondition.notify_all();
        emitInitialClientReadyIfReady();
        return generation;
    }

    void shutdownAppSrc()
    {
        std::vector<GstAppSrc*> previous;
        {
            std::lock_guard lock(_appSrcMutex);
            previous.reserve(_appSrcs.size());
            for (const auto& binding : _appSrcs) {
                if (binding.source != nullptr) {
                    previous.push_back(binding.source);
                }
            }
            _appSrcs.clear();
            _appSrcGeneration.clear();
            if (!previous.empty()) {
                bumpAppSrcSetGenerationLocked();
            }
        }
        _appSrcCondition.notify_all();
        for (GstAppSrc* source : previous) {
            gst_object_unref(source);
        }
    }

    void retireAppSrc(AppSrcGeneration generation)
    {
        GstAppSrc* previous = nullptr;
        {
            std::lock_guard lock(_appSrcMutex);
            const auto binding = std::find_if(
                _appSrcs.begin(), _appSrcs.end(),
                [generation](const AppSrcBinding& candidate) { return candidate.generation == generation; });
            if (binding == _appSrcs.end()) {
                return;
            }
            previous = binding->source;
            _appSrcs.erase(binding);
            static_cast<void>(_appSrcGeneration.retire(generation));
            bumpAppSrcSetGenerationLocked();
        }
        _appSrcCondition.notify_all();
        if (previous != nullptr) {
            gst_object_unref(previous);
        }
    }

    void bumpAppSrcSetGenerationLocked()
    {
        if (_appSrcSetGeneration < std::numeric_limits<std::uint64_t>::max()) {
            ++_appSrcSetGeneration;
        }
    }

    [[nodiscard]] std::optional<AppSrcBatchLease> waitForAppSrcAfter(
        std::uint64_t previousAppSrcSetGeneration, std::uint64_t previousClientAvailabilityGeneration,
        std::chrono::steady_clock::time_point deadline, std::uint32_t requiredAppSrcCount,
        const std::vector<AppSrcGeneration>& excludedGenerations = {})
    {
        // In shared-media mode one appsrc is fanned out by GStreamer to all
        // connected RTSP clients.  In the default non-shared mode the caller
        // still requires one appsrc per client/generation.
        const std::uint32_t effectiveRequiredAppSrcCount = _sharedFactory ? 1U : requiredAppSrcCount;
        std::unique_lock lock(_appSrcMutex);
        const bool ready = _appSrcCondition.wait_until(
            lock, deadline,
            [this, previousAppSrcSetGeneration, previousClientAvailabilityGeneration,
             effectiveRequiredAppSrcCount] {
                const bool newerIdentity = _appSrcSetGeneration > previousAppSrcSetGeneration
                    || _clientAvailabilityGeneration > previousClientAvailabilityGeneration;
                return (_clientCount >= _options.minimumClientCount
                        && _appSrcs.size() >= effectiveRequiredAppSrcCount
                        && newerIdentity)
                    || _finished.load();
            });
        const bool newerIdentity = _appSrcSetGeneration > previousAppSrcSetGeneration
            || _clientAvailabilityGeneration > previousClientAvailabilityGeneration;
        if (!ready || _clientCount < _options.minimumClientCount
            || _appSrcs.size() < effectiveRequiredAppSrcCount
            || !newerIdentity || _finished.load()) {
            return std::nullopt;
        }
        AppSrcBatchLease batch;
        batch.clientCount = _clientCount;
        batch.appSrcCount = static_cast<std::uint32_t>(_appSrcs.size());
        batch.clientAvailabilityGeneration = _clientAvailabilityGeneration;
        batch.appSrcSetGeneration = _appSrcSetGeneration;
        batch.sources.reserve(_appSrcs.size());
        for (const auto& binding : _appSrcs) {
            if (binding.source == nullptr
                || std::find(excludedGenerations.begin(), excludedGenerations.end(), binding.generation)
                       != excludedGenerations.end()) {
                continue;
            }
            batch.sources.push_back(AppSrcLease{GST_APP_SRC(gst_object_ref(binding.source)), binding.generation,
                                                _clientAvailabilityGeneration, _appSrcSetGeneration});
        }
        return batch;
    }

    static void releaseBatch(AppSrcBatchLease& batch)
    {
        for (auto& lease : batch.sources) {
            if (lease.source != nullptr) {
                gst_object_unref(lease.source);
                lease.source = nullptr;
            }
        }
        batch.sources.clear();
    }

    [[nodiscard]] GstBuffer* makeFrameBuffer(const Message& message, std::uint64_t ptsNs,
                                             std::string& error)
    {
        GstBuffer* buffer = gst_buffer_new_allocate(nullptr, static_cast<gsize>(message.payload.size()), nullptr);
        if (buffer == nullptr) {
            error = "failed to allocate a GStreamer frame buffer";
            return nullptr;
        }
        GstMapInfo map{};
        if (!gst_buffer_map(buffer, &map, GST_MAP_WRITE)) {
            gst_buffer_unref(buffer);
            error = "failed to map the GStreamer frame buffer";
            return nullptr;
        }
        if (map.size != message.payload.size()) {
            gst_buffer_unmap(buffer, &map);
            gst_buffer_unref(buffer);
            error = "mapped GStreamer frame buffer did not preserve the exact payload size";
            return nullptr;
        }
        std::memcpy(map.data, message.payload.data(), message.payload.size());
        gst_buffer_unmap(buffer, &map);

        GST_BUFFER_PTS(buffer) = ptsNs;
        GST_BUFFER_DTS(buffer) = ptsNs;
        GST_BUFFER_DURATION(buffer) = GST_CLOCK_TIME_NONE;
        GST_BUFFER_OFFSET(buffer) = message.header.sequence;
        return buffer;
    }

    void readerLoop()
    {
        while (!_finished.load()) {
            Message message;
            std::string error;
            const auto readStatus = MultiDetectReplayRtsp::readMessage(std::cin, message, error);
            if (readStatus != MultiDetectReplayRtsp::ReadStatus::Ok) {
                fail("wire_read", error);
                return;
            }

            const std::uint64_t processingStartedNs = monotonicNowNs();
            if (!_validator.validate(message, error)) {
                fail("wire_validation", error);
                return;
            }

            if (message.header.type == MessageType::EndOfStream) {
                finishEndOfStream();
                return;
            }

            if (!_havePtsOrigin) {
                _ptsOriginCapturedAtNs = message.header.capturedAtNs;
                _havePtsOrigin = true;
            }
            const std::uint64_t ptsNs = message.header.capturedAtNs - _ptsOriginCapturedAtNs;
            const auto appSrcDeadline = std::chrono::steady_clock::now()
                + std::chrono::milliseconds(_options.clientTimeoutMs);
            std::uint32_t generationRetryCount = 0;
            std::uint32_t fanoutAttempts = 0;
            std::uint32_t lastClientCount = 0;
            std::uint64_t lastAppSrcSetGeneration = 0;
            std::uint64_t lastClientAvailabilityGeneration = 0;
            AppSrcGeneration lastDeliveredGeneration = 0;
            std::vector<AppSrcGeneration> deliveredGenerations;
            GstBuffer* frameBuffer = makeFrameBuffer(message, ptsNs, error);
            if (frameBuffer == nullptr) {
                fail("buffer_prepare", error);
                return;
            }
            bool initialPartialAttempt = true;
            while (true) {
                const std::uint32_t requiredAppSrcCount = initialPartialAttempt
                    ? 1U
                    : std::max<std::uint32_t>(1U, _options.minimumClientCount);
                std::optional<AppSrcBatchLease> batch = waitForAppSrcAfter(
                    lastAppSrcSetGeneration, lastClientAvailabilityGeneration, appSrcDeadline, requiredAppSrcCount,
                    deliveredGenerations);
                if (!batch.has_value()) {
                    gst_buffer_unref(frameBuffer);
                    fail("client_timeout", "no RTSP media client became ready before the configured timeout");
                    return;
                }
                lastClientCount = batch->clientCount;
                lastAppSrcSetGeneration = batch->appSrcSetGeneration;
                lastClientAvailabilityGeneration = batch->clientAvailabilityGeneration;
                if (batch->sources.empty()) {
                    if ((_sharedFactory || batch->appSrcCount >= _options.minimumClientCount)
                        && deliveredGenerations.size() >= batch->appSrcCount) {
                        // Every remaining source belongs to a generation that
                        // already accepted this frame; a disconnected source
                        // does not require duplicating the frame.
                        break;
                    }
                    initialPartialAttempt = false;
                    continue;
                }

                ++fanoutAttempts;
                std::uint32_t failedPushes = 0;
                GstFlowReturn lastFlow = GST_FLOW_OK;
                for (const AppSrcLease& lease : batch->sources) {
                    // appsrc takes ownership of each reference.  Keep the
                    // original until every active media pipeline has had a
                    // chance to consume one identical validated frame,
                    // including a media pipeline that appears after the
                    // initial non-shared DESCRIBE is unblocked.
                    GstBuffer* clientBuffer = gst_buffer_ref(frameBuffer);
                    const GstFlowReturn flow = gst_app_src_push_buffer(lease.source, clientBuffer);
                    lastFlow = flow;
                    if (flow == GST_FLOW_OK) {
                        if (std::find(deliveredGenerations.begin(), deliveredGenerations.end(), lease.generation)
                            == deliveredGenerations.end()) {
                            deliveredGenerations.push_back(lease.generation);
                        }
                        lastDeliveredGeneration = lease.generation;
                    } else {
                        ++failedPushes;
                    }
                }
                releaseBatch(*batch);

                if (failedPushes == 0) {
                    if ((_sharedFactory || batch->appSrcCount >= _options.minimumClientCount)
                        && deliveredGenerations.size() >= batch->appSrcCount) {
                        break;
                    }
                    // A non-shared RTSP factory may expose the next appsrc
                    // only after this first frame unblocks its predecessor.
                    // Keep the same buffer alive and wait for that topology
                    // generation before acknowledging the frame.
                    initialPartialAttempt = false;
                    if (++generationRetryCount > kMaximumGenerationRetriesPerMessage) {
                        gst_buffer_unref(frameBuffer);
                        fail("push_buffer", "appsrc fan-out did not reach the configured client minimum");
                        return;
                    }
                    continue;
                }

                // A receiver rebuild can retire one media pipeline while
                // another client is still active.  Retry only failed or new
                // generations after the active-set generation changes; the
                // successful generations are excluded to avoid duplicate
                // frames on healthy clients.
                if (++generationRetryCount > kMaximumGenerationRetriesPerMessage) {
                    gst_buffer_unref(frameBuffer);
                    fail("push_buffer",
                         std::string("appsrc push-buffer did not stabilize across reconnects; last flow=")
                             + gst_flow_get_name(lastFlow));
                    return;
                }
                initialPartialAttempt = false;
            }

            gst_buffer_unref(frameBuffer);

            const std::uint64_t acceptedNs = monotonicNowNs();
            const std::uint64_t processingLatencyNs = acceptedNs - processingStartedNs;
            _maxProcessingLatencyNs = std::max(_maxProcessingLatencyNs, processingLatencyNs);
            if (_frameCount == 0) {
                _firstCapturedAtNs = message.header.capturedAtNs;
            }
            _lastCapturedAtNs = message.header.capturedAtNs;
            ++_frameCount;
            _totalPayloadBytes += message.header.payloadBytes;

            JsonEvent accepted("frame_accepted");
            accepted.number("sequence", message.header.sequence);
            accepted.number("captured_at_ns", message.header.capturedAtNs);
            accepted.number("accepted_monotonic_ns", acceptedNs);
            accepted.number("pts_ns", ptsNs);
            accepted.number("payload_bytes", message.header.payloadBytes);
            accepted.number("processing_latency_ns", processingLatencyNs);
            accepted.number("generation", lastDeliveredGeneration);
            accepted.number("client_availability_generation", lastClientAvailabilityGeneration);
            accepted.number("generation_retry_count", generationRetryCount);
            accepted.number("fanout_attempts", fanoutAttempts);
            accepted.number("fanout_count", _sharedFactory ? lastClientCount : deliveredGenerations.size());
            accepted.boolean("shared_media", _sharedFactory);
            accepted.number("client_count", lastClientCount);
            emitEvent(std::move(accepted));
        }
    }

    void finishEndOfStream()
    {
        const auto appSrcDeadline = std::chrono::steady_clock::now()
            + std::chrono::milliseconds(_options.clientTimeoutMs);
        std::uint64_t appSrcSetGeneration = 0;
        std::uint64_t clientAvailabilityGeneration = 0;
        std::vector<AppSrcGeneration> completedGenerations;
        for (std::uint32_t retry = 0; retry <= kMaximumGenerationRetriesPerMessage; ++retry) {
            std::optional<AppSrcBatchLease> batch = waitForAppSrcAfter(
                appSrcSetGeneration, clientAvailabilityGeneration, appSrcDeadline,
                std::max<std::uint32_t>(1U, _options.minimumClientCount), completedGenerations);
            if (!batch.has_value()) {
                fail("eos_appsrc", "no RTSP client was present before EOS submission timed out");
                return;
            }
            appSrcSetGeneration = batch->appSrcSetGeneration;
            clientAvailabilityGeneration = batch->clientAvailabilityGeneration;
            if (batch->sources.empty()) {
                if (!completedGenerations.empty()) {
                    complete("eos", 0, std::nullopt, std::nullopt, true);
                    return;
                }
                continue;
            }

            std::uint32_t failedSubmissions = 0;
            GstFlowReturn lastFlow = GST_FLOW_OK;
            for (const AppSrcLease& lease : batch->sources) {
                const GstFlowReturn flow = gst_app_src_end_of_stream(lease.source);
                lastFlow = flow;
                if (flow == GST_FLOW_OK) {
                    if (std::find(completedGenerations.begin(), completedGenerations.end(), lease.generation)
                        == completedGenerations.end()) {
                        completedGenerations.push_back(lease.generation);
                    }
                } else {
                    ++failedSubmissions;
                }
            }
            releaseBatch(*batch);
            if (failedSubmissions == 0) {
                complete("eos", 0, std::nullopt, std::nullopt, true);
                return;
            }
            if (retry == kMaximumGenerationRetriesPerMessage) {
                fail("eos_flow", std::string("appsrc end-of-stream did not stabilize; last flow=")
                                     + gst_flow_get_name(lastFlow));
                return;
            }
        }
        fail("eos_flow", "too many RTSP media generations changed while submitting EOS");
    }

    void fail(std::string code, std::string message)
    {
        complete("error", 2, std::move(code), std::move(message), false);
    }

    void complete(std::string_view reason, int exitCode, std::optional<std::string> errorCode,
                  std::optional<std::string> errorMessage, bool drain)
    {
        bool expected = false;
        if (!_finished.compare_exchange_strong(expected, true)) {
            return;
        }
        _exitCode.store(exitCode);
        _appSrcCondition.notify_all();

        if (errorCode.has_value()) {
            JsonEvent error("error");
            error.string("code", *errorCode);
            error.string("message", errorMessage.value_or("unknown publisher error"));
            error.number("frame_count", _frameCount);
            emitEvent(std::move(error));
        }

        JsonEvent summary("summary");
        summary.string("reason", reason);
        summary.number("frame_count", _frameCount);
        summary.number("bytes", _totalPayloadBytes);
        summary.number("first_captured_at_ns", _firstCapturedAtNs);
        summary.number("last_captured_at_ns", _lastCapturedAtNs);
        summary.number("max_processing_latency_ns", _maxProcessingLatencyNs);
        summary.number("exit_code", exitCode);
        emitEvent(std::move(summary));

        if (_loop != nullptr) {
            GSourceFunc callback = drain ? &Publisher::scheduleDrainCallback : &Publisher::quitLoopCallback;
            g_main_context_invoke(nullptr, callback, this);
        }
    }

    Options _options;
    MultiDetectReplayRtsp::SequenceValidator _validator;
    GMainLoop* _loop = nullptr;
    GstRTSPServer* _server = nullptr;
    guint _sourceId = 0;
    std::thread _reader;
    std::mutex _appSrcMutex;
    std::condition_variable _appSrcCondition;
    std::vector<AppSrcBinding> _appSrcs;
    MultiDetectReplayRtsp::AppSrcGenerationState _appSrcGeneration;
    bool _sharedFactory = false;
    std::uint32_t _clientCount = 0;
    std::uint64_t _clientAvailabilityGeneration = 0;
    std::uint64_t _appSrcSetGeneration = 0;
    bool _initialClientReadyEmitted = false;
    std::atomic<bool> _finished{false};
    std::atomic<int> _exitCode{2};
    std::string _encoder;
    bool _havePtsOrigin = false;
    std::uint64_t _ptsOriginCapturedAtNs = 0;
    std::uint64_t _frameCount = 0;
    std::uint64_t _totalPayloadBytes = 0;
    std::uint64_t _firstCapturedAtNs = 0;
    std::uint64_t _lastCapturedAtNs = 0;
    std::uint64_t _maxProcessingLatencyNs = 0;
};

}  // namespace

int main(int argc, char* argv[])
{
#ifdef _WIN32
    if (_setmode(_fileno(stdin), _O_BINARY) == -1) {
        JsonEvent event("error");
        event.string("code", "stdin_binary_mode");
        event.string("message", "failed to switch stdin to binary mode");
        emitEvent(std::move(event));
        return 2;
    }
#endif

    Options options;
    std::string error;
    if (!parseOptions(argc, argv, options, error)) {
        if (error.empty()) {
            return 0;
        }
        JsonEvent event("error");
        event.string("code", "arguments");
        event.string("message", error);
        emitEvent(std::move(event));
        printUsage();
        return 2;
    }

    GError* gstError = nullptr;
    if (!gst_init_check(&argc, &argv, &gstError)) {
        JsonEvent event("error");
        event.string("code", "gstreamer_init");
        event.string("message", gstError == nullptr ? "gst_init_check failed" : gstError->message);
        emitEvent(std::move(event));
        if (gstError != nullptr) {
            g_error_free(gstError);
        }
        return 2;
    }

    int exitCode = 2;
    {
        Publisher publisher(std::move(options));
        if (!publisher.start(error)) {
            if (publisher.exitCode() == 2 && !error.empty()) {
                JsonEvent event("error");
                event.string("code", "server_start");
                event.string("message", error);
                emitEvent(std::move(event));
            }
            exitCode = publisher.exitCode();
        } else {
            exitCode = publisher.exitCode();
        }
    }
    // gst_deinit() is intentionally omitted. It is a process-global leak-check
    // helper, not required for normal applications, and can wait on an RTSP
    // session whose peer disappeared while this short-lived helper is exiting.
    return exitCode;
}
