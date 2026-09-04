void Resource::release() { released_ = true; delete this; }
void Resource::use() { payload_[0] = 1; }
