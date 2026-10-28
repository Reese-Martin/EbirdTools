#pragma once
#include <cstdint>
#include <vector>
#include <memory>
#include <stdexcept>
#include <fcntl.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <unistd.h>

inline std::shared_ptr<void> map_readonly(const std::string& path,size_t bytes) {
    int fd=open(path.c_str(),O_RDONLY);struct stat st;
    if(fd<0 || fstat(fd,&st)!=0 || size_t(st.st_size)!=bytes){if(fd>=0)close(fd);throw std::runtime_error("missing/truncated compact catalog: "+path);}
    void* data=mmap(nullptr,bytes,PROT_READ,MAP_SHARED,fd,0);close(fd);
    if(data==MAP_FAILED)throw std::runtime_error("cannot map compact catalog");
    return std::shared_ptr<void>(data,[bytes](void* p){munmap(p,bytes);});
}
struct SpeciesSpan {
    const uint32_t* data;size_t count;
    const uint32_t* begin()const{return data;}
    const uint32_t* end()const{return data+count;}
};
struct Opportunity {int day,location;SpeciesSpan species;uint32_t variant=0;};
struct OpportunityRecord {uint32_t day,location;uint64_t offset;uint32_t count,reserved;};
static_assert(sizeof(OpportunityRecord)==24,"compact record ABI");
struct OpportunityCatalog {
    std::vector<OpportunityRecord> owned_records;
    std::vector<uint32_t> owned_species;
    std::shared_ptr<void> records_owner,species_owner;
    const OpportunityRecord* records=nullptr;const uint32_t* species=nullptr;size_t count=0;
    Opportunity operator[](size_t i)const {
        const auto&r=records[i];return {int(r.day),int(r.location),{species+r.offset,r.count},r.reserved};
    }
    struct Iterator {
        const OpportunityCatalog* catalog;size_t index;
        Opportunity operator*()const{return (*catalog)[index];}
        Iterator& operator++(){++index;return *this;}
        bool operator!=(const Iterator&other)const{return index!=other.index;}
    };
    Iterator begin()const{return {this,0};}Iterator end()const{return {this,count};}
    void owned(){records=owned_records.data();species=owned_species.data();count=owned_records.size();}
    void mapped(const std::string& rec,const std::string& spp,size_t n,size_t entries,int days,int nloc,int nspp){
        records_owner=map_readonly(rec,n*sizeof(OpportunityRecord));species_owner=map_readonly(spp,entries*sizeof(uint32_t));
        records=static_cast<const OpportunityRecord*>(records_owner.get());species=static_cast<const uint32_t*>(species_owner.get());count=n;
        uint64_t expected=0;
        for(size_t i=0;i<n;++i){const auto&r=records[i];
            if(r.day>=uint32_t(days)||r.location>=uint32_t(nloc)||r.offset!=expected||r.count>entries-expected)
                throw std::runtime_error("invalid compact opportunity");
            if(i && (r.day<records[i-1].day || (r.day==records[i-1].day &&
                (r.location<records[i-1].location || (r.location==records[i-1].location &&
                 (!r.reserved || r.reserved<=records[i-1].reserved))))))throw std::runtime_error("noncanonical compact catalog");
            for(size_t j=0;j<r.count;++j)if(species[r.offset+j]>=uint32_t(nspp) || (j&&species[r.offset+j]<=species[r.offset+j-1]))throw std::runtime_error("invalid compact species");
            expected+=r.count;
        }
        if(expected!=entries)throw std::runtime_error("unused compact species entries");
    }
};
