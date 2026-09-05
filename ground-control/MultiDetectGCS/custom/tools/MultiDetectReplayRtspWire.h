#pragma once

#include <array>
#include <cstddef>
#include <cstdint>
#include <istream>
#include <span>
#include <string>
#include <string_view>
#include <vector>

namespace MultiDetectReplayRtsp {

inline constexpr std::array<std::uint8_t, 8> kMagic{'M', 'D', 'R', 'T', 'S', 'P', '1', 0};
inline constexpr std::uint16_t kVersion = 1;
inline constexpr std::uint32_t kHeaderBytes = 88;
inline constexpr std::uint32_t kPixelFormatBgr24 = 1;
inline constexpr std::uint32_t kMaxWidth = 8192;
inline constexpr std::uint32_t kMaxHeight = 8192;
inline constexpr std::uint64_t kMaxPayloadBytes = 128ULL * 1024ULL * 1024ULL;

enum class MessageType : std::uint16_t {
    Frame = 1,
    EndOfStream = 2,
};

struct Header {
    MessageType type = MessageType::Frame;
    std::uint64_t sequence = 0;
    std::uint64_t capturedAtNs = 0;
    std::uint32_t width = 0;
    std::uint32_t height = 0;
    std::uint32_t stride = 0;
    std::uint32_t pixelFormat = 0;
    std::uint64_t payloadBytes = 0;
    std::array<std::uint8_t, 32> sha256{};
};

struct Message {
    Header header;
    std::vector<std::uint8_t> payload;
};

enum class ReadStatus {
    Ok,
    CleanEndOfFile,
    TruncatedHeader,
    TruncatedPayload,
    IoError,
    InvalidHeader,
};

[[nodiscard]] std::array<std::uint8_t, kHeaderBytes> encodeHeader(const Header& header);
[[nodiscard]] bool decodeHeader(std::span<const std::uint8_t> bytes, Header& header, std::string& error);
[[nodiscard]] std::array<std::uint8_t, 32> sha256(std::span<const std::uint8_t> bytes);
[[nodiscard]] ReadStatus readMessage(std::istream& stream, Message& message, std::string& error);
[[nodiscard]] bool validRtspMount(std::string_view mount, std::string& error);
[[nodiscard]] bool validH264Geometry(std::uint32_t width, std::uint32_t height, std::string& error);

class SequenceValidator {
public:
    SequenceValidator(std::uint32_t width, std::uint32_t height);

    [[nodiscard]] bool validConfiguration(std::string& error) const;
    [[nodiscard]] bool validate(Message& message, std::string& error);

    [[nodiscard]] std::uint64_t nextSequence() const { return _nextSequence; }
    [[nodiscard]] std::uint64_t lastCapturedAtNs() const { return _lastCapturedAtNs; }
    [[nodiscard]] bool hasFrame() const { return _hasFrame; }
    [[nodiscard]] bool sawEndOfStream() const { return _sawEndOfStream; }
    [[nodiscard]] std::uint32_t width() const { return _width; }
    [[nodiscard]] std::uint32_t height() const { return _height; }
    [[nodiscard]] std::uint32_t stride() const { return _stride; }
    [[nodiscard]] std::uint64_t frameBytes() const { return _frameBytes; }

private:
    std::uint32_t _width = 0;
    std::uint32_t _height = 0;
    std::uint32_t _stride = 0;
    std::uint64_t _frameBytes = 0;
    std::uint64_t _nextSequence = 0;
    std::uint64_t _lastCapturedAtNs = 0;
    bool _hasFrame = false;
    bool _sawEndOfStream = false;
};

}  // namespace MultiDetectReplayRtsp
