#pragma once

#include <cstdint>
#include <limits>
#include <optional>

namespace MultiDetectReplayRtsp {

// Monotonic identity for successive GstRTSPMedia/appsrc instances.  The
// publisher protects this state with the same mutex as its appsrc pointer.
// Keeping the identity separate from the pointer prevents a delayed
// "unprepared" callback from an old media instance from clearing a newer one.
class AppSrcGenerationState {
public:
    using Generation = std::uint64_t;

    [[nodiscard]] std::optional<Generation> publish() noexcept
    {
        if (_lastPublished == std::numeric_limits<Generation>::max()) {
            return std::nullopt;
        }
        _current = ++_lastPublished;
        return _current;
    }

    [[nodiscard]] bool retire(Generation generation) noexcept
    {
        if (generation == 0 || generation != _current) {
            return false;
        }
        _current = 0;
        return true;
    }

    void clear() noexcept { _current = 0; }

    [[nodiscard]] Generation current() const noexcept { return _current; }
    [[nodiscard]] Generation lastPublished() const noexcept { return _lastPublished; }
    [[nodiscard]] bool isCurrent(Generation generation) const noexcept
    {
        return generation != 0 && generation == _current;
    }

private:
    Generation _current = 0;
    Generation _lastPublished = 0;
};

}  // namespace MultiDetectReplayRtsp
