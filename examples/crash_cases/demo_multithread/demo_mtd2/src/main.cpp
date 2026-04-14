#include "mylib.h"

int main() {
    std::cout << "=== 数据处理系统启动 ===" << std::endl;
    std::cout << "正在初始化数据处理引擎..." << std::endl;
    
    setup_crash_handler();
    
    g_data_manager = new DataManager();
    
    const int num_threads = 8;
    const int operations_per_thread = 1000;
    
    std::vector<std::thread> threads;
    
    for (int i = 0; i < num_threads; ++i) {
        threads.emplace_back(process_data_thread, i, operations_per_thread);
    }
    
    std::thread memory_thread(memory_cleanup_thread);
    std::thread sync_thread(data_sync_thread);
    
    std::cout << "数据处理引擎运行中，预计处理 " << (operations_per_thread * num_threads) << " 个数据项..." << std::endl;
    
    auto start_time = std::chrono::high_resolution_clock::now();
    
    for (auto& t : threads) {
        t.join();
    }
    
    auto end_time = std::chrono::high_resolution_clock::now();
    auto duration = std::chrono::duration_cast<std::chrono::milliseconds>(end_time - start_time);
    
    std::cout << "数据处理完成，耗时: " << duration.count() << "ms" << std::endl;
    std::cout << "总处理数: " << g_operation_count.load() << std::endl;
    std::cout << "数据节点数: " << g_data_manager->get_node_count() << std::endl;
    
    g_running.store(false);
    
    memory_thread.join();
    sync_thread.join();
    
    delete g_data_manager;
    g_data_manager = nullptr;
    
    std::cout << "数据处理系统正常关闭" << std::endl;
    return 0;
}
