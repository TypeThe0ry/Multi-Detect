#include "MultiDetectReplayRtspGeneration.h"
#include "MultiDetectReplayRtspWire.h"

#include <array>
#include <cstdint>
#include <cstdlib>
#include <iostream>
#include <sstream>
#include <string>

namespace {

using MultiDetectReplayRtsp::Header;
using MultiDetectReplayRtsp::Message;
using MultiDetectReplayRtsp::MessageType;

[[noreturn]] void fail(const std::string& message)
{
    std::cerr << "MultiDetectReplayRtspWireSelfTest: " << message << '\n';
    std::exit(1);
}

void require(bool condition, const std::string& message)
{
    if (!condition) {
        fail(message);
    }
}

Message makeFrame(std::uint64_t sequence, std::uint64_t capturedAtNs, std::uint32_t width = 4,
                  std::uint32_t height = 3)
{
    Message message;
    message.header.type = MessageType::Frame;
    message.header.sequence = sequence;
    message.header.capturedAtNs = capturedAtNs;
    message.header.width = width;
    message.header.height = height;
    message.header.stride = width * 3U;
    message.header.pixelFormat = MultiDetectReplayRtsp::kPixelFormatBgr24;
    message.header.payloadBytes = static_cast<std::uint64_t>(message.header.stride) * height;
    message.payload.resize(static_cast<std::size_t>(message.header.payloadBytes));
    for (std::size_t index = 0; index < message.payload.size(); ++index) {
        message.payload[index] = static_cast<std::uint8_t>((index + sequence) & 0xFFU);
    }
    message.header.sha256 = MultiDetectReplayRtsp::sha256(message.payload);
    return message;
}

Message makeEos(std::uint64_t sequence, std::uint64_t capturedAtNs, std::uint32_t width = 4,
                std::uint32_t height = 3)
{
    Message message;
    message.header.type = MessageType::EndOfStream;
    message.header.sequence = sequence;
    message.header.capturedAtNs = capturedAtNs;
    message.header.width = width;
    message.header.height = height;
    message.header.stride = width * 3U;
    message.header.pixelFormat = MultiDetectReplayRtsp::kPixelFormatBgr24;
    return message;
}

std::string serialize(const Message& message)
{
    const auto header = MultiDetectReplayRtsp::encodeHeader(message.header);
    std::string result(reinterpret_cast<const char*>(header.data()), header.size());
    if (!message.payload.empty()) {
        result.append(reinterpret_cast<const char*>(message.payload.data()), message.payload.size());
    }
    return result;
}

void testParseAndSequence()
{
    const Message frame0 = makeFrame(0, 100);
    const Message frame1 = makeFrame(1, 200);
    const Message eos = makeEos(2, 200);
    std::istringstream input(serialize(frame0) + serialize(frame1) + serialize(eos),
                             std::ios::in | std::ios::binary);

    MultiDetectReplayRtsp::SequenceValidator validator(4, 3);
    std::string error;
    require(validator.validConfiguration(error), error);
    for (int index = 0; index < 3; ++index) {
        Message parsed;
        require(MultiDetectReplayRtsp::readMessage(input, parsed, error) == MultiDetectReplayRtsp::ReadStatus::Ok,
                "valid message did not parse: " + error);
        require(validator.validate(parsed, error), "valid message failed validation: " + error);
    }
    require(validator.sawEndOfStream(), "EOS state was not recorded");
    require(validator.nextSequence() == 2, "accepted frame count changed on EOS");
}

void testGeometryAndMonotonicity()
{
    MultiDetectReplayRtsp::SequenceValidator validator(4, 3);
    std::string error;
    Message frame = makeFrame(0, 100);
    require(validator.validate(frame, error), error);

    Message badSize = makeFrame(1, 200, 5, 3);
    require(!validator.validate(badSize, error) && error.find("width") != std::string::npos,
            "geometry mismatch was accepted");

    Message nonMonotonic = makeFrame(1, 100);
    require(!validator.validate(nonMonotonic, error) && error.find("strictly increasing") != std::string::npos,
            "non-monotonic captured_at_ns was accepted");
}

void testTruncation()
{
    const Message frame = makeFrame(0, 100);
    const std::string bytes = serialize(frame);
    std::string error;
    Message parsed;

    std::istringstream shortHeader(bytes.substr(0, 30), std::ios::in | std::ios::binary);
    require(MultiDetectReplayRtsp::readMessage(shortHeader, parsed, error)
                == MultiDetectReplayRtsp::ReadStatus::TruncatedHeader,
            "truncated header was not rejected");

    std::istringstream shortPayload(bytes.substr(0, bytes.size() - 1), std::ios::in | std::ios::binary);
    require(MultiDetectReplayRtsp::readMessage(shortPayload, parsed, error)
                == MultiDetectReplayRtsp::ReadStatus::TruncatedPayload,
            "truncated payload was not rejected");
}

void testHardUpperBoundAndChecksum()
{
    Header header = makeFrame(0, 100).header;
    header.payloadBytes = MultiDetectReplayRtsp::kMaxPayloadBytes + 1;
    const auto encoded = MultiDetectReplayRtsp::encodeHeader(header);
    Header decoded;
    std::string error;
    require(!MultiDetectReplayRtsp::decodeHeader(encoded, decoded, error) && error.find("hard") != std::string::npos,
            "payload above the hard upper bound was accepted");

    MultiDetectReplayRtsp::SequenceValidator validator(4, 3);
    Message corrupt = makeFrame(0, 100);
    corrupt.payload.front() ^= 0x80U;
    require(!validator.validate(corrupt, error) && error.find("SHA-256") != std::string::npos,
            "payload checksum mismatch was accepted");
}

void testStrictEos()
{
    MultiDetectReplayRtsp::SequenceValidator validator(4, 3);
    std::string error;
    Message frame = makeFrame(0, 100);
    require(validator.validate(frame, error), error);

    Message badEos = makeEos(1, 99);
    require(!validator.validate(badEos, error) && error.find("EOS captured_at_ns") != std::string::npos,
            "EOS with the wrong capture timestamp was accepted");

    Message eos = makeEos(1, 100);
    require(validator.validate(eos, error), error);
    Message afterEos = makeFrame(1, 200);
    require(!validator.validate(afterEos, error) && error.find("after EOS") != std::string::npos,
            "message after EOS was accepted");
}

void testMountAndEncoderGeometry()
{
    std::string error;
    require(MultiDetectReplayRtsp::validRtspMount("/multidetect-replay", error), error);
    require(MultiDetectReplayRtsp::validRtspMount("/replay.v1/camera~front", error), error);
    for (const std::string_view invalid : {"/", "/replay/", "/replay//front", "/.", "/..", "/a/../b",
                                           "/replay?token"}) {
        require(!MultiDetectReplayRtsp::validRtspMount(invalid, error),
                "unsafe RTSP mount was accepted: " + std::string(invalid));
    }

    require(MultiDetectReplayRtsp::validH264Geometry(1920, 1080, error), error);
    require(!MultiDetectReplayRtsp::validH264Geometry(1919, 1080, error)
                && error.find("even") != std::string::npos,
            "odd H.264 width was accepted");
    require(!MultiDetectReplayRtsp::validH264Geometry(1920, 1079, error)
                && error.find("even") != std::string::npos,
            "odd H.264 height was accepted");
    require(!MultiDetectReplayRtsp::validH264Geometry(MultiDetectReplayRtsp::kMaxWidth,
                                                       MultiDetectReplayRtsp::kMaxHeight, error)
                && error.find("hard") != std::string::npos,
            "H.264 geometry exceeding the frame-byte bound was accepted");
}

void testAppSrcGenerationIdentity()
{
    MultiDetectReplayRtsp::AppSrcGenerationState state;
    require(state.current() == 0 && state.lastPublished() == 0,
            "appsrc generation state did not start empty");

    const auto first = state.publish();
    require(first.has_value() && *first == 1 && state.isCurrent(*first),
            "first appsrc generation was not published");
    const auto second = state.publish();
    require(second.has_value() && *second == 2 && state.isCurrent(*second),
            "replacement appsrc generation was not published");

    require(!state.retire(*first),
            "delayed retirement from an old media generation cleared the replacement");
    require(state.isCurrent(*second),
            "old media retirement changed the current appsrc generation");
    require(state.retire(*second) && state.current() == 0,
            "current media generation did not retire cleanly");

    const auto third = state.publish();
    require(third.has_value() && *third == 3,
            "generation identity was reused after a disconnect/reconnect cycle");
    state.clear();
    require(state.current() == 0 && state.lastPublished() == 3,
            "final clear reset the monotonic generation identity");
}

}  // namespace

int main(int argc, char* argv[])
{
    static_cast<void>(argc);
    static_cast<void>(argv);
    testParseAndSequence();
    testGeometryAndMonotonicity();
    testTruncation();
    testHardUpperBoundAndChecksum();
    testStrictEos();
    testMountAndEncoderGeometry();
    testAppSrcGenerationIdentity();
    std::cout << "MultiDetect replay RTSP wire self-test passed\n";
    return 0;
}
