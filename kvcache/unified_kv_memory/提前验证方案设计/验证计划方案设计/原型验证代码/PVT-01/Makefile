CXX = g++
CXXFLAGS = -O3 -std=c++17 -pthread -Wall

TARGET = raw_trans_bench
SRCS = raw_trans_bench.cc

all: $(TARGET)

$(TARGET): $(SRCS)
	$(CXX) $(CXXFLAGS) -o $(TARGET) $(SRCS)

clean:
	rm -f $(TARGET) *.csv
