void Cache::flush() { lock(worker_mutex_); lock(cache_mutex_); }
