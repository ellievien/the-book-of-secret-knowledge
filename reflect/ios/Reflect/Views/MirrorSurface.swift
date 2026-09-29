import SwiftUI
import UIKit

/// The live picture plus touch handling:
/// tap = click, long press = right click, one-finger drag = scroll (with a short fling),
/// pinch / two-finger drag = local zoom and pan only.
struct MirrorSurface: UIViewRepresentable {
    let image: UIImage?
    let onTap: (CGPoint) -> Void
    let onLongPress: (CGPoint) -> Void
    let onScroll: (CGPoint, CGVector) -> Void

    func makeUIView(context: Context) -> MirrorSurfaceView {
        MirrorSurfaceView()
    }

    func updateUIView(_ view: MirrorSurfaceView, context: Context) {
        view.onTap = onTap
        view.onLongPress = onLongPress
        view.onScroll = onScroll
        view.setImage(image)
    }
}

final class MirrorSurfaceView: UIView, UIScrollViewDelegate {
    var onTap: (CGPoint) -> Void = { _ in }
    var onLongPress: (CGPoint) -> Void = { _ in }
    var onScroll: (CGPoint, CGVector) -> Void = { _, _ in }

    private let scrollView = UIScrollView()
    private let imageView = UIImageView()
    private var imageSize: CGSize = .zero
    private var fittedSize: CGSize = .zero

    private var scrollAnchor: CGPoint?
    private var pendingDelta: CGPoint = .zero
    private var lastScrollSent: CFTimeInterval = 0
    private var inertiaLink: CADisplayLink?
    private var inertiaVelocity: CGPoint = .zero
    private var inertiaStarted: CFTimeInterval = 0
    private var lastInertiaTick: CFTimeInterval = 0
    private var ignoreTapsUntil: CFTimeInterval = 0
    private let haptic = UIImpactFeedbackGenerator(style: .medium)

    override init(frame: CGRect) {
        super.init(frame: frame)
        backgroundColor = .black

        scrollView.delegate = self
        scrollView.minimumZoomScale = 1
        scrollView.maximumZoomScale = 4
        scrollView.bouncesZoom = true
        scrollView.showsHorizontalScrollIndicator = false
        scrollView.showsVerticalScrollIndicator = false
        scrollView.contentInsetAdjustmentBehavior = .never
        scrollView.backgroundColor = .black
        scrollView.delaysContentTouches = false
        // One finger scrolls the desktop; panning the zoomed picture takes two.
        scrollView.panGestureRecognizer.minimumNumberOfTouches = 2
        addSubview(scrollView)

        imageView.contentMode = .scaleToFill
        imageView.isUserInteractionEnabled = true
        imageView.accessibilityLabel = "Claude desktop window"
        scrollView.addSubview(imageView)

        let tap = UITapGestureRecognizer(target: self, action: #selector(handleTap(_:)))
        let longPress = UILongPressGestureRecognizer(target: self, action: #selector(handleLongPress(_:)))
        longPress.minimumPressDuration = 0.45
        let pan = UIPanGestureRecognizer(target: self, action: #selector(handlePan(_:)))
        pan.maximumNumberOfTouches = 1
        for recognizer in [tap, longPress, pan] {
            imageView.addGestureRecognizer(recognizer)
        }
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) { fatalError("init(coder:) is not used") }

    override func layoutSubviews() {
        super.layoutSubviews()
        if scrollView.frame != bounds {
            scrollView.frame = bounds
            relayout(force: true)
        }
    }

    override func willMove(toWindow newWindow: UIWindow?) {
        super.willMove(toWindow: newWindow)
        if newWindow == nil { stopInertia() }
    }

    func setImage(_ image: UIImage?) {
        guard imageView.image !== image else { return }
        imageView.image = image
        guard let image else { return }
        if image.size != imageSize {
            imageSize = image.size
            relayout(force: false)
        }
    }

    // MARK: Layout (aspect fit, black letterbox, zoom)

    private func relayout(force: Bool) {
        guard imageSize.width > 0, bounds.width > 0, bounds.height > 0 else { return }
        let fitted = MirrorGeometry.aspectFit(imageSize, in: CGRect(origin: .zero, size: bounds.size)).size
        guard force || abs(fitted.width - fittedSize.width) > 0.5 || abs(fitted.height - fittedSize.height) > 0.5 else { return }
        fittedSize = fitted
        scrollView.zoomScale = 1
        imageView.frame = CGRect(origin: .zero, size: fitted)
        scrollView.contentSize = fitted
        centerImage()
    }

    private func centerImage() {
        let offsetX = max((scrollView.bounds.width - scrollView.contentSize.width) / 2, 0)
        let offsetY = max((scrollView.bounds.height - scrollView.contentSize.height) / 2, 0)
        imageView.center = CGPoint(x: scrollView.contentSize.width / 2 + offsetX,
                                   y: scrollView.contentSize.height / 2 + offsetY)
    }

    func viewForZooming(in scrollView: UIScrollView) -> UIView? { imageView }

    func scrollViewDidZoom(_ scrollView: UIScrollView) { centerImage() }

    func scrollViewDidEndZooming(_ scrollView: UIScrollView, with view: UIView?, atScale scale: CGFloat) {
        if scale < 1.05 { scrollView.setZoomScale(1, animated: true) }
    }

    // MARK: Gestures

    private func normalized(_ point: CGPoint) -> CGPoint? {
        MirrorGeometry.normalized(point, in: imageView.bounds.size)
    }

    @objc private func handleTap(_ recognizer: UITapGestureRecognizer) {
        guard recognizer.state == .ended, CACurrentMediaTime() >= ignoreTapsUntil,
              let point = normalized(recognizer.location(in: imageView)) else { return }
        stopInertia()
        onTap(point)
    }

    @objc private func handleLongPress(_ recognizer: UILongPressGestureRecognizer) {
        guard recognizer.state == .began, let point = normalized(recognizer.location(in: imageView)) else { return }
        ignoreTapsUntil = CACurrentMediaTime() + 0.8
        haptic.impactOccurred()
        onLongPress(point)
    }

    @objc private func handlePan(_ recognizer: UIPanGestureRecognizer) {
        switch recognizer.state {
        case .began:
            stopInertia()
            let location = recognizer.location(in: imageView)
            let moved = recognizer.translation(in: imageView)
            let start = CGPoint(x: location.x - moved.x, y: location.y - moved.y)
            let size = imageView.bounds.size
            scrollAnchor = normalized(start) ?? CGPoint(x: min(max(start.x / max(size.width, 1), 0), 1),
                                                        y: min(max(start.y / max(size.height, 1), 0), 1))
            pendingDelta = moved
            recognizer.setTranslation(.zero, in: imageView)
            flushScroll(force: false)
        case .changed:
            let moved = recognizer.translation(in: imageView)
            recognizer.setTranslation(.zero, in: imageView)
            pendingDelta.x += moved.x
            pendingDelta.y += moved.y
            flushScroll(force: false)
        case .ended:
            flushScroll(force: true)
            startInertia(recognizer.velocity(in: imageView))
        default:
            flushScroll(force: true)
            scrollAnchor = nil
        }
    }

    /// Sends accumulated finger movement at most 60 times a second.
    private func flushScroll(force: Bool) {
        let now = CACurrentMediaTime()
        guard let anchor = scrollAnchor, pendingDelta != .zero, force || now - lastScrollSent >= 1.0 / 60 else { return }
        onScroll(anchor, MirrorGeometry.normalizedDelta(pendingDelta, in: imageView.bounds.size))
        pendingDelta = .zero
        lastScrollSent = now
    }

    private func startInertia(_ velocity: CGPoint) {
        guard hypot(velocity.x, velocity.y) > 250 else {
            scrollAnchor = nil
            return
        }
        inertiaVelocity = velocity
        inertiaStarted = CACurrentMediaTime()
        lastInertiaTick = inertiaStarted
        let link = CADisplayLink(target: self, selector: #selector(inertiaTick(_:)))
        link.preferredFrameRateRange = CAFrameRateRange(minimum: 30, maximum: 60, preferred: 60)
        link.add(to: .main, forMode: .common)
        inertiaLink = link
    }

    @objc private func inertiaTick(_ link: CADisplayLink) {
        let now = CACurrentMediaTime()
        let dt = now - lastInertiaTick
        lastInertiaTick = now
        let decay = pow(0.94, dt * 60)
        inertiaVelocity.x *= decay
        inertiaVelocity.y *= decay
        pendingDelta.x += inertiaVelocity.x * dt
        pendingDelta.y += inertiaVelocity.y * dt
        flushScroll(force: true)
        if hypot(inertiaVelocity.x, inertiaVelocity.y) < 40 || now - inertiaStarted > 1.5 {
            stopInertia()
        }
    }

    private func stopInertia() {
        inertiaLink?.invalidate()
        inertiaLink = nil
        inertiaVelocity = .zero
        pendingDelta = .zero
    }
}
