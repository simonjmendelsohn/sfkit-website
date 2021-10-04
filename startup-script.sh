#!/bin/bash

# Sanity check that the startup-script is working
touch /home/test.txt

# Many of the commands need root privileges for the VM
sudo -s

echo "\n\n Begin installing dependencies \n\n"
apt-get --assume-yes update
apt-get --assume-yes install build-essential
apt-get --assume-yes install clang-3.9
apt-get --assume-yes install libgmp3-dev
apt-get --assume-yes install libssl-dev
apt-get --assume-yes install libomp-dev
apt-get --assume-yes install netcat
apt-get --assume-yes install git
apt-get --assume-yes install python3-pip
pip3 install numpy
pip3 install google-cloud-pubsub
echo "\n\n Done installing dependencies \n\n"

echo "\n\n Setting up pubsub to let web server know the progress \n\n"
cd /home
git clone https://github.com/simonjmendelsohn/secure-gwas-pubsub /home/secure-gwas-pubsub
python3 secure-gwas-pubsub/publish.py done_installing_dependencies

echo "\n\n Begin installing GWAS repo \n\n"
cd /home
git clone https://github.com/simonjmendelsohn/secure-gwas /home/secure-gwas
echo "\n\n Done installing GWAS repo \n\n"

echo "\n\n Begin installing NTL library \n\n"
curl https://libntl.org/ntl-10.3.0.tar.gz --output ntl-10.3.0.tar.gz
tar -zxvf ntl-10.3.0.tar.gz
cp secure-gwas/code/NTL_mod/ZZ.h ntl-10.3.0/include/NTL/
cp secure-gwas/code/NTL_mod/ZZ.cpp ntl-10.3.0/src/
cd ntl-10.3.0/src
./configure NTL_THREAD_BOOST=on
make all
make install
cd /home
python3 secure-gwas-pubsub/publish.py done_installing_NTL
echo "\n\n Done installing NTL library \n\n"

echo "\n\n Begin compiling secure gwas code \n\n"
cd /home/secure-gwas/code
COMP=$(which clang++)
sed -i "s|^CPP.*$|CPP = ${COMP}|g" Makefile
sed -i "s|^INCPATHS.*$|INCPATHS = -I/usr/local/include|g" Makefile
sed -i "s|^LDPATH.*$|LDPATH = -L/usr/local/lib|g" Makefile
make
cd /home
python3 secure-gwas-pubsub/publish.py done_compiling_gwas
echo "\n\n done compiling secure gwas code \n\n"

echo "\n\n Waiting for all other VMs to be ready for GWAS \n\n"
role=$(hostname | tail -c 2)
nc -k -l -p 8055 &
for i in 0 1 2 3; do
    false
    while [ $? == 1 ]; do
        echo "Waiting for VM secure-gwas${i} to be done setting up"
        sleep 30
        nc -w 5 -v -z 10.0.0.1${i} 8055 &>/dev/null
    done
done
python3 secure-gwas-pubsub/publish.py all_vms_are_ready
echo "\n\n All VMs are ready to begin GWAS \n\n"

echo "\n\n Starting DataSharing and GWAS \n\n"
cd /home/secure-gwas/code
sleep $((5 * ${role}))
if [[ $role -eq "3" ]]; then
    bin/DataSharingClient ${role} ../par/test.par.${role}.txt ../test_data/
    cd /home
    python3 secure-gwas-pubsub/publish.py DataSharing_completed
else
    bin/DataSharingClient ${role} ../par/test.par.${role}.txt

    python3 /home/secure-gwas-pubsub/publish.py DataSharing_completed

    echo "\n\n Waiting a couple minutes between DataSharing and GWAS... \n\n"
    sleep $((120 + 15 * ${role}))
    bin/GwasClient ${role} ../par/test.par.${role}.txt

    cd /home
    python3 secure-gwas-pubsub/publish.py GWAS_completed
fi
echo "\n\n Done with GWAS \n\n"
