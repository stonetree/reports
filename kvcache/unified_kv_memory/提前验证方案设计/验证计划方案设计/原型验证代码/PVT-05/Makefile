CXX = g++
CXXFLAGS = -O3 -std=c++17 -pthread -Wall

TARGET = tier_storage_bench
SRCS = tier_storage_bench.cc

all: $(TARGET)

$(TARGET): $(SRCS)
	$(CXX) $(CXXFLAGS) -o $(TARGET) $(SRCS)

clean:
	rm -f $(TARGET) *.csv
