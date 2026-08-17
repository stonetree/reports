CXX = g++
CXXFLAGS = -O3 -std=c++17 -pthread -Wall

TARGET = view_vs_copy_bench
SRCS = view_vs_copy_bench.cc view_guard.cc

all: $(TARGET)

$(TARGET): $(SRCS)
	$(CXX) $(CXXFLAGS) -o $(TARGET) $(SRCS)

clean:
	rm -f $(TARGET) *.csv
