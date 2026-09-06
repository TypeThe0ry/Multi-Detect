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
            _retiredEpochs.append(_activeEpoch);
            constexpr int kMaximumRetiredEpochs = 32;
            while (_retiredEpochs.size() > kMaximumRetiredEpochs) {
                _retiredEpochs.removeFirst();
            }
        }
        _activeEpoch = epoch;
        return Result::Advanced;
    }

    quint32 activeEpoch() const { return _activeEpoch; }
    int retiredCount() const { return _retiredEpochs.size(); }

private:
    quint32 _activeEpoch = 0;
    QList<quint32> _retiredEpochs;
};
