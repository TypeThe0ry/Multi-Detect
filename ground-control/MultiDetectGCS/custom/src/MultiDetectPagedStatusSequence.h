#pragma once

#include <QtCore/QHash>
#include <QtCore/QtGlobal>

// Paged status packets share one message type but each page is an independent
// UDP datagram. Sequence checks must therefore be per page: page 1 may arrive
// before page 0 without making page 0 a stale packet.
namespace MultiDetectPagedStatusSequence {

inline bool acceptPageSequence(QHash<int, quint32>* lastSequenceByPage, int pageIndex, quint32 sequence)
{
    if (!lastSequenceByPage || pageIndex < 0) {
        return false;
    }
    const auto existing = lastSequenceByPage->constFind(pageIndex);
    if (existing != lastSequenceByPage->constEnd() && static_cast<qint32>(sequence - existing.value()) <= 0) {
        return false;
    }
    lastSequenceByPage->insert(pageIndex, sequence);
    return true;
}

}  // namespace MultiDetectPagedStatusSequence
