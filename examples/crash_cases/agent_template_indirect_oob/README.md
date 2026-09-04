# Template indirect out-of-bounds

The generic `VectorView::at` frame is a decoy until the agent follows the length field through `FrameParser`.
