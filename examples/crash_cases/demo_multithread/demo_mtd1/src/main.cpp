#include "mylib.h"

int main() {
    std::cout << "=== 复杂多线程数据损坏模拟程序 ===" << std::endl;
    std::cout << "这个程序故意制造多线程竞态条件来触发内存损坏..." << std::endl;
    
    // 设置崩溃处理器
    setup_crash_handler();
    
    // 创建共享数据结构
    g_shared_data = new ComplexDataStructure();
    
    // 创建多个工作线程
    const int num_threads = 8;
    const int operations_per_thread = 1000;
    
    std::vector<std::thread> threads;
    
    // 启动工作线程
    for (int i = 0; i < num_threads; ++i) {
        threads.emplace_back(worker_thread, i, operations_per_thread);
    }
    
    // 启动内存压力测试线程
    std::thread memory_thread(memory_pressure_thread);
    
    // 启动内存损坏线程
    std::thread corruption_thread_obj(corruption_thread);
    
    // 运行一段时间
    std::cout << "程序运行中，预计 " << (operations_per_thread * num_threads) << " 次操作..." << std::endl;
    
    auto start_time = std::chrono::high_resolution_clock::now();
    
    // 等待所有工作线程完成
    for (auto& t : threads) {
        t.join();
    }
    
    auto end_time = std::chrono::high_resolution_clock::now();
    auto duration = std::chrono::duration_cast<std::chrono::milliseconds>(end_time - start_time);
    
    std::cout << "工作线程完成，耗时: " << duration.count() << "ms" << std::endl;
    std::cout << "总操作数: " << g_operation_count.load() << std::endl;
    std::cout << "节点数: " << g_shared_data->get_node_count() << std::endl;
    
    // 停止其他线程
    g_running.store(false);
    
    memory_thread.join();
    corruption_thread_obj.join();
    
    // 清理
    delete g_shared_data;
    g_shared_data = nullptr;
    
    std::cout << "程序正常结束" << std::endl;
    return 0;
}
