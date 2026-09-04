// The callback remains queued after release; the fix belongs at this boundary.
void Session::onCallback() { resource_->use(); }
