void CallbackQueue::flush() { for (auto& callback : callbacks_) callback(); }
