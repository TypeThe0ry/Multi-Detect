#pragma once

#include <QtCore/QList>

// Small transport-independent state machine for type-26 producer restarts.
// Once an epoch advances, every previously active epoch is retired and can no
// longer become active again, even if an authenticated UDP datagram arrives
// late or out of order.
class MultiDetectObservationEpoch final
{
public:
    enum class Result
    {
        Invalid,
        Current,
        Advanced,
        Retired,
        CapacityExhausted,
    };

    Result observe(quint32 epoch)
    {
        if (epoch == 0) {
            return Result::Invalid;
        }
        if (_activeEpoch == epoch) {
            return Result::Current;
        }
        if (_retiredEpochs.contains(epoch)) {
            return Result::Retired;
        }
        if (_activeEpoch != 0) {
            // Never evict a retired epoch: eviction could let a very delayed
            // authenticated datagram reactivate after enough producer
            // restarts. Bound memory by refusing the 33rd session transition
            // until the QGC controller itself is restarted.
            if (_retiredEpochs.size() >= kMaximumRetiredEpochs) {
                return Result::CapacityExhausted;
            }
            _retiredEpochs.append(_activeEpoch);
        }
        _activeEpoch = epoch;
        return Result::Advanced;
    }

    quint32 activeEpoch() const { return _activeEpoch; }
    int retiredCount() const { return _retiredEpochs.size(); }
    static constexpr int maximumRetiredEpochs() { return kMaximumRetiredEpochs; }

private:
    static constexpr int kMaximumRetiredEpochs = 32;
    quint32 _activeEpoch = 0;
    QList<quint32> _retiredEpochs;
};
