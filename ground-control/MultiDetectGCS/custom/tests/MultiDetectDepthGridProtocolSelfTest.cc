#include "MultiDetectDepthGridProtocol.h"

#include <QtCore/QFile>
#include <QtCore/QString>

#include <iostream>

namespace {

QByteArray readFile(const QString& path)
{
    QFile file(path);
    if (!file.open(QIODevice::ReadOnly)) {
        return {};
    }
    return file.readAll();
}

}  // namespace

int main(int argc, char* argv[])
{
    if (argc < 3) {
        std::cerr << "usage: depth-grid-self-test EXPECTED_RAW DATAGRAM...\n";
        return 2;
    }
    const QByteArray expectedRaw = readFile(QString::fromLocal8Bit(argv[1]));
    if (expectedRaw.isEmpty()) {
        std::cerr << "expected raw grid is empty\n";
        return 3;
    }
    MultiDetectDepthGridProtocol protocol(QByteArray(32, 'k'));
    std::optional<MultiDetectDepthGridFrame> completed;
    for (int index = 2; index < argc; ++index) {
        const QByteArray datagram = readFile(QString::fromLocal8Bit(argv[index]));
        QString error;
        const std::optional<MultiDetectDepthGridFrame> candidate =
            protocol.ingest(datagram, 1000U + static_cast<quint64>(index), &error);
        if (!error.isEmpty()) {
            std::cerr << error.toStdString() << '\n';
            return 4;
        }
        if (candidate.has_value()) {
            completed = candidate;
        }
    }
    if (!completed.has_value() || completed->width != 160 || completed->height != 90 ||
        completed->quantizedDepth != expectedRaw || completed->minimumDepthM != 1.25 ||
        completed->maximumDepthM != 24.5) {
        std::cerr << "reassembled depth-grid frame differs from Python wire fixture\n";
        return 5;
    }
    std::cout << "depth-grid Python/C++ wire round-trip passed\n";
    return 0;
}

